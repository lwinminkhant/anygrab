import os
import re
import traceback
import time
import tempfile
import uuid
import asyncio
import anyio
import glob as glob_module
import hashlib
import logging
from dotenv import load_dotenv
from collections import OrderedDict
from contextlib import AsyncExitStack, asynccontextmanager
from typing import List, Optional, Dict, Any
from pathlib import Path

load_dotenv()

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from curl_cffi.requests import AsyncSession, Session
from pydantic import BaseModel, field_validator
import yt_dlp
from yt_dlp.networking.impersonate import ImpersonateTarget
from account_api import router as account_router, shutdown_accounts
from learning_api import router as learning_router, shutdown_learning
from media_source import VIDEO_DOWNLOAD_FORMAT as _VIDEO_DOWNLOAD_FORMAT
from public_media_safety import (SafeYoutubeDL, PublicSession, PublicAsyncSession,
                                 validate_url, validate_headers, safe_download_path)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("anygrab")
_warned_impersonation_unavailable = False

def _is_impersonation_unavailable_error(err: Exception) -> bool:
    msg = str(err).lower()
    return "impersonate target" in msg and "not available" in msg


def _warn_impersonation_unavailable_once(err: Exception):
    global _warned_impersonation_unavailable
    if not _warned_impersonation_unavailable:
        log.warning("Impersonation target unavailable, falling back to default client: %s", err)
        _warned_impersonation_unavailable = True


COOKIES_FILE = os.getenv("COOKIES_FILE", "cookies.txt")
PROXY_URL = os.getenv("PROXY_URL", "").strip() or None


def _has_valid_cookies(cookie_path: Optional[str] = None) -> bool:
    path = cookie_path or COOKIES_FILE
    if not path or not os.path.isfile(path):
        return False
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    return True
    except Exception:
        pass
    return False


def _apply_common_ydl_opts(opts: dict) -> dict:
    updated = dict(opts)
    if PROXY_URL:
        updated["proxy"] = PROXY_URL
    if _has_valid_cookies():
        updated["cookiefile"] = COOKIES_FILE
    return updated


def _new_session(**kwargs) -> Session:
    if PROXY_URL and "proxy" not in kwargs:
        kwargs["proxy"] = PROXY_URL
    try:
        return PublicSession(impersonate="chrome", **kwargs)
    except Exception as e:
        if _is_impersonation_unavailable_error(e):
            _warn_impersonation_unavailable_once(e)
            return PublicSession(**kwargs)
        raise


def _new_async_session(**kwargs) -> AsyncSession:
    if PROXY_URL and "proxy" not in kwargs:
        kwargs["proxy"] = PROXY_URL
    return PublicAsyncSession(**kwargs)

DOWNLOAD_DIR = Path.home() / "Downloads" / "AnyGrab"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
MAX_CONCURRENT_EXTRACTIONS = int(os.getenv("MAX_EXTRACTIONS", "6"))
MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_DOWNLOADS", "4"))
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL", "300"))
CACHE_MAX_SIZE = int(os.getenv("CACHE_MAX", "256"))
RATE_LIMIT_REQUESTS = int(os.getenv("RATE_LIMIT_REQ", "30"))
RATE_LIMIT_WINDOW = int(os.getenv("RATE_LIMIT_WIN", "60"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "120"))

# ---------------------------------------------------------------------------
# Concurrency gates
# ---------------------------------------------------------------------------
_extract_semaphore: asyncio.Semaphore = None  # type: ignore[assignment]
_download_semaphore: asyncio.Semaphore = None  # type: ignore[assignment]
_active_extractions = 0
_active_downloads = 0
_total_requests = 0

# ---------------------------------------------------------------------------
# LRU + TTL cache for extraction results
# ---------------------------------------------------------------------------
class TTLCache:
    __slots__ = ("_max", "_ttl", "_store")

    def __init__(self, maxsize: int, ttl: int):
        self._max = maxsize
        self._ttl = ttl
        self._store: OrderedDict[str, tuple[float, Any]] = OrderedDict()

    def get(self, key: str):
        entry = self._store.get(key)
        if entry is None:
            return None
        ts, val = entry
        if time.monotonic() - ts > self._ttl:
            self._store.pop(key, None)
            return None
        self._store.move_to_end(key)
        return val

    def set(self, key: str, val: Any):
        self._store[key] = (time.monotonic(), val)
        self._store.move_to_end(key)
        while len(self._store) > self._max:
            self._store.popitem(last=False)

    @property
    def size(self):
        return len(self._store)

_extract_cache = TTLCache(CACHE_MAX_SIZE, CACHE_TTL_SECONDS)

# ---------------------------------------------------------------------------
# Rate limiter (sliding window per IP)
# ---------------------------------------------------------------------------
class RateLimiter:
    __slots__ = ("_limit", "_window", "_clients")

    def __init__(self, limit: int, window: int):
        self._limit = limit
        self._window = window
        self._clients: Dict[str, list[float]] = {}

    def is_allowed(self, key: str) -> bool:
        now = time.monotonic()
        hits = self._clients.get(key, [])
        hits = [t for t in hits if now - t < self._window]
        if len(hits) >= self._limit:
            self._clients[key] = hits
            return False
        hits.append(now)
        self._clients[key] = hits
        return True

    def cleanup(self):
        now = time.monotonic()
        stale = [k for k, v in self._clients.items() if all(now - t > self._window for t in v)]
        for k in stale:
            del self._clients[k]

_rate_limiter = RateLimiter(RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW)

# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _extract_semaphore, _download_semaphore
    _extract_semaphore = asyncio.Semaphore(MAX_CONCURRENT_EXTRACTIONS)
    _download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
    cookie_status = "active" if _has_valid_cookies() else "none"
    proxy_status = "active" if PROXY_URL else "none"
    log.info(
        "AnyGrab ready  |  extractions=%d  downloads=%d  cache=%ds/%d  rate=%d/%ds  cookies=%s  proxy=%s",
        MAX_CONCURRENT_EXTRACTIONS, MAX_CONCURRENT_DOWNLOADS,
        CACHE_TTL_SECONDS, CACHE_MAX_SIZE,
        RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW,
        cookie_status, proxy_status,
    )
    try:
        yield
    finally:
        await asyncio.gather(shutdown_learning(), shutdown_accounts())
        log.info("AnyGrab shutting down")

_PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
_fastapi_kw: Dict[str, Any] = {
    "title": "Universal Social Media Downloader API",
    "description": "API to extract metadata, captions, and media URLs from various social platforms.",
    "lifespan": lifespan,
}
if _PUBLIC_URL:
    _fastapi_kw["servers"] = [{"url": _PUBLIC_URL, "description": "Public"}]

app = FastAPI(**_fastapi_kw)
app.include_router(account_router)
app.include_router(learning_router)

_cors_origins = os.getenv("CORS_ORIGINS", "*").strip()
if _cors_origins == "*":
    _allow_origins = ["*"]
else:
    _allow_origins = [o.strip() for o in _cors_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allow_origins,
    allow_credentials=_allow_origins != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_trusted_hosts = os.getenv("TRUSTED_HOSTS", "").strip()
if _trusted_hosts and _trusted_hosts != "*":
    _th_list = [h.strip() for h in _trusted_hosts.split(",") if h.strip()]
    # Docker healthchecks use Host: localhost; the bot uses http://api:8000 (Host: api).
    # Starlette TrustedHostMiddleware splits Host on the first ":" only, so 127.0.0.1:8000 is read as
    # "127", not "127.0.0.1" — include "127" so loopback IPv4 works.
    for _h in ("localhost", "127.0.0.1", "127", "[::1]", "api"):
        if _h not in _th_list:
            _th_list.append(_h)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=_th_list)

# ---------------------------------------------------------------------------
# Middleware: rate limiting + request timeout + metrics
# ---------------------------------------------------------------------------
@app.middleware("http")
async def rate_limit_and_timeout(request: Request, call_next):
    global _total_requests
    _total_requests += 1

    if request.url.path.startswith("/api/") and request.url.path != "/api/v1/health":
        client_ip = request.client.host if request.client else "unknown"
        if not _rate_limiter.is_allowed(client_ip):
            return JSONResponse(
                {"detail": "Rate limit exceeded. Please slow down."},
                status_code=429,
                headers={"Retry-After": str(RATE_LIMIT_WINDOW)},
            )
        try:
            # Account downloads include a bounded source fetch and a Drive upload.
            timeout = max(REQUEST_TIMEOUT, 600) if request.url.path == "/api/v1/drive" else REQUEST_TIMEOUT
            return await asyncio.wait_for(call_next(request), timeout=timeout)
        except asyncio.TimeoutError:
            return JSONResponse({"detail": "Request timed out."}, status_code=504)

    return await call_next(request)

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class URLRequest(BaseModel):
    url: str

    @field_validator("url")
    @classmethod
    def public_source(cls, value):
        return validate_url(value, source=True)

class DownloadRequest(BaseModel):
    url: str
    headers: Dict[str, str] = {}
    original_url: Optional[str] = None
    audio_only: bool = False

    @field_validator("url")
    @classmethod
    def public_media(cls, value):
        return validate_url(value)

    @field_validator("original_url")
    @classmethod
    def public_source(cls, value):
        return validate_url(value, source=True) if value is not None else None

    @field_validator("headers")
    @classmethod
    def media_headers(cls, value):
        return validate_headers(value)

class MediaResponse(BaseModel):
    platform: str
    caption: Optional[str] = None
    media_urls: List[str]
    metadata: Dict[str, Any]
    http_headers: Dict[str, str] = {}

class SaveRequest(DownloadRequest):
    filename: Optional[str] = None

    @field_validator("filename")
    @classmethod
    def safe_filename(cls, value):
        if value is not None:
            safe_download_path(DOWNLOAD_DIR, value)
        return value

class SaveResponse(BaseModel):
    success: bool
    filename: str
    path: str
    size: int

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _cache_key(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()

def get_platform(url: str) -> str:
    from urllib.parse import urlsplit
    host = (urlsplit(url).hostname or "").lower()
    domains = {"youtube": ("youtube.com", "youtu.be"), "tiktok": ("tiktok.com",),
               "x": ("twitter.com", "x.com"), "facebook": ("facebook.com", "fb.watch"),
               "instagram": ("instagram.com",)}
    return next((platform for platform, hosts in domains.items()
                 if any(host == item or host.endswith("." + item) for item in hosts)), "unknown")

def _run_ytdlp_extract(url: str, ydl_opts: dict) -> dict:
    with SafeYoutubeDL(ydl_opts, single_video=get_platform(url) == "youtube") as ydl:
        return ydl.extract_info(url, download=False)


def _run_ytdlp_extract_with_fallback(url: str, ydl_opts: dict) -> dict:
    try:
        return _run_ytdlp_extract(url, ydl_opts)
    except Exception as e:
        if ydl_opts.get("impersonate") and _is_impersonation_unavailable_error(e):
            _warn_impersonation_unavailable_once(e)
            retry_opts = dict(ydl_opts)
            retry_opts.pop("impersonate", None)
            return _run_ytdlp_extract(url, retry_opts)
        raise


def _merge_extractor_args(base: dict, extra: dict) -> dict:
    merged = dict(base or {})
    for key, value in (extra or {}).items():
        current = merged.get(key, {})
        next_value = dict(current) if isinstance(current, dict) else {}
        next_value.update(value)
        merged[key] = next_value
    return merged


# Keep yt-dlp's maintained defaults; Safari may additionally expose combined HLS.
# android_vr now requires PO tokens for its HTTPS formats and is no longer a default.
_YOUTUBE_FALLBACK_CLIENTS = ["default", "web_safari"]
_YOUTUBE_PREVIEW_FORMAT = "best[vcodec^=h264]/best[vcodec^=avc]/best/bestvideo[vcodec^=avc]/bestvideo"


def _youtube_public_ydl_variants(base_opts: dict) -> list[dict]:
    """Try maintained defaults, then add Safari without replacing those defaults."""
    variants = [dict(base_opts)]

    v_alt = dict(base_opts)
    v_alt.pop("impersonate", None)
    v_alt["extractor_args"] = _merge_extractor_args(
        v_alt.get("extractor_args", {}),
        {"youtube": {"player_client": _YOUTUBE_FALLBACK_CLIENTS}},
    )
    variants.append(v_alt)

    return variants

def _build_media_response(info: dict, platform: str) -> MediaResponse:
    caption = info.get("description") or info.get("title")
    media_urls = []
    if "url" in info:
        media_urls.append(info["url"])
    elif "entries" in info:
        for entry in info["entries"]:
            if "url" in entry:
                media_urls.append(entry["url"])

    clean_metadata = {
        "id": info.get("id"),
        "uploader": info.get("uploader"),
        "upload_date": info.get("upload_date"),
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "title": info.get("title"),
    }
    return MediaResponse(
        platform=platform,
        caption=caption,
        media_urls=media_urls,
        metadata=clean_metadata,
        http_headers=info.get("http_headers", {}),
    )

def _youtube_failure_detail(err: Optional[Exception]) -> str:
    """Turn yt-dlp's failure into advice that matches how this service runs."""
    if err is not None and "not a bot" in str(err).lower():
        if _has_valid_cookies():
            return (
                "YouTube bot check failed even with cookies. The exported cookies may have expired, "
                "or YouTube is blocking this IP address. Please refresh cookies.txt or route via PROXY_URL."
            )
        return (
            "YouTube is rate-limiting this server's IP address (bot check). "
            "Please export cookies from YouTube into cookies.txt, or configure a residential PROXY_URL in .env."
        )
    return f"Failed to extract youtube data: {err}"


def extract_with_ytdlp(url: str, platform: str, *, allow_tiktok_fallback: bool = True) -> MediaResponse:
    validate_url(url, source=True)
    ydl_opts = _apply_common_ydl_opts({
        "skip_download": True,
        "noplaylist": True,
        "format": _YOUTUBE_PREVIEW_FORMAT if platform == "youtube" else "best[vcodec^=h264]/best[vcodec^=avc]/best",
        "impersonate": ImpersonateTarget(client="chrome"),
    })

    try:
        if platform == "youtube":
            last_err = None
            for idx, variant in enumerate(_youtube_public_ydl_variants(ydl_opts), start=1):
                try:
                    info = _run_ytdlp_extract_with_fallback(url, variant)
                    break
                except Exception as e:
                    last_err = e
                    log.info("YouTube public extraction strategy %s failed: %s", idx, e)
            else:
                raise HTTPException(status_code=400, detail=_youtube_failure_detail(last_err))
        else:
            info = _run_ytdlp_extract_with_fallback(url, ydl_opts)
    except HTTPException:
        # Already a deliberate, caller-facing message -- re-wrapping would nest it ("...: 400: ...").
        raise
    except Exception as first_err:
        if platform == "tiktok" and allow_tiktok_fallback:
            log.info("yt-dlp TikTok failed, trying fallback: %s", first_err)
            return extract_tiktok_fallback(url)
        log.error("Extraction error: %s", traceback.format_exc())
        raise HTTPException(status_code=400, detail=f"Failed to extract {platform} data: {first_err}")

    return _build_media_response(info, platform)

def extract_tiktok_fallback(url: str) -> MediaResponse:
    validate_url(url, source=True)
    try:
        with _new_session() as session:
            resp = session.get("https://www.tikwm.com/api/", params={"url": url, "hd": 1}, timeout=15)
            resp.raise_for_status()
            data = resp.json()

        if data.get("code") != 0:
            raise ValueError(f"tikwm API error: {data.get('msg', 'unknown')}")

        d = data["data"]
        images = d.get("images", [])
        is_photo = bool(images)

        media_urls = []
        if is_photo:
            media_urls = images
        else:
            if d.get("hdplay"):
                media_urls.append(d["hdplay"])
            elif d.get("play"):
                media_urls.append(d["play"])

        if not media_urls:
            raise ValueError("No media URLs returned from tikwm API")

        return MediaResponse(
            platform="tiktok",
            caption=d.get("title"),
            media_urls=media_urls,
            metadata={
                "id": str(d.get("id", "")),
                "uploader": d.get("author", {}).get("unique_id"),
                "upload_date": None,
                "view_count": d.get("play_count"),
                "like_count": d.get("digg_count"),
                "duration": d.get("duration") if not is_photo else None,
                "is_video": not is_photo,
                "thumbnail": d.get("cover"),
            },
            http_headers={},
        )
    except HTTPException:
        raise
    except Exception as e:
        log.error("TikTok fallback error: %s", traceback.format_exc())
        raise HTTPException(status_code=400, detail=f"Failed to extract TikTok data: {e}")

def _shortcode_to_media_id(shortcode: str) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    media_id = 0
    for char in shortcode:
        media_id = media_id * 64 + alphabet.index(char)
    return str(media_id)

def _get_instagram_session() -> Session:
    return _new_session()

def extract_instagram(url: str) -> MediaResponse:
    validate_url(url, source=True)
    match = re.search(r"(?:p|reel|tv)/([^/?#&]+)", url)
    if not match:
        raise HTTPException(status_code=400, detail="Invalid Instagram URL. Must be a post or reel.")

    shortcode = match.group(1)
    media_id = _shortcode_to_media_id(shortcode)

    try:
        s = _get_instagram_session()
        api_url = f"https://www.instagram.com/api/v1/media/{media_id}/info/"
        resp = s.get(api_url, headers={
            "X-IG-App-ID": "936619743392459",
            "X-Requested-With": "XMLHttpRequest",
        }, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        item = data["items"][0]
        caption_data = item.get("caption") or {}
        caption = caption_data.get("text", "") if isinstance(caption_data, dict) else ""
        user = item.get("user", {})
        is_video = item.get("media_type") == 2

        media_urls = []
        carousel = item.get("carousel_media", [])
        if carousel:
            for cm in carousel:
                vid_versions = cm.get("video_versions", [])
                if vid_versions:
                    media_urls.append(vid_versions[0]["url"])
                else:
                    candidates = cm.get("image_versions2", {}).get("candidates", [])
                    if candidates:
                        best = max(candidates, key=lambda c: c.get("width", 0) * c.get("height", 0))
                        media_urls.append(best["url"])
        elif is_video:
            vid_versions = item.get("video_versions", [])
            if vid_versions:
                media_urls.append(vid_versions[0]["url"])
        else:
            candidates = item.get("image_versions2", {}).get("candidates", [])
            if candidates:
                best = max(candidates, key=lambda c: c.get("width", 0) * c.get("height", 0))
                media_urls.append(best["url"])

        if not media_urls:
            raise ValueError("No media URLs found in API response")

        metadata = {
            "id": shortcode,
            "owner_username": user.get("username"),
            "upload_date": item.get("taken_at"),
            "likes": item.get("like_count"),
            "comments": item.get("comment_count"),
            "is_video": is_video,
            "thumbnail": item.get("image_versions2", {}).get("candidates", [{}])[0].get("url") if not carousel else None,
        }

        return MediaResponse(platform="instagram", caption=caption, media_urls=media_urls, metadata=metadata)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to extract Instagram data: {e}")

# ---------------------------------------------------------------------------
# Temp file cleanup helper
# ---------------------------------------------------------------------------
def _cleanup_files(pattern: str):
    for f in glob_module.glob(pattern):
        try:
            os.remove(f)
        except OSError:
            pass

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.post("/api/v1/extract", response_model=MediaResponse)
async def extract_social_media(request: URLRequest):
    global _active_extractions
    platform = get_platform(request.url)
    if platform == "unknown":
        raise HTTPException(status_code=400, detail="Unsupported platform or invalid URL")

    cache_key = _cache_key(request.url)
    cached = _extract_cache.get(cache_key)
    if cached is not None:
        log.info("Cache hit for %s", request.url[:80])
        return cached

    async with _extract_semaphore:
        _active_extractions += 1
        try:
            if platform == "instagram":
                try:
                    result = await asyncio.to_thread(extract_with_ytdlp, request.url, platform)
                except Exception as e:
                    log.info("yt-dlp Instagram failed, trying API fallback: %s", e)
                    result = await asyncio.to_thread(extract_instagram, request.url)
            elif platform == "tiktok":
                try:
                    result = await asyncio.to_thread(extract_tiktok_fallback, request.url)
                except HTTPException:
                    log.info("TikTok provider unavailable, trying direct extraction")
                    result = await asyncio.to_thread(
                        extract_with_ytdlp, request.url, platform, allow_tiktok_fallback=False,
                    )
            else:
                result = await asyncio.to_thread(extract_with_ytdlp, request.url, platform)

            _extract_cache.set(cache_key, result)
            return result
        finally:
            _active_extractions -= 1


@app.post("/api/v1/download")
async def proxy_download(request: DownloadRequest):
    global _active_downloads
    platform = get_platform(request.original_url) if request.original_url else "unknown"

    async with _download_semaphore:
        _active_downloads += 1
        try:
            if platform in ("instagram", "tiktok"):
                return await _proxy_stream_download(request)

            if request.original_url:
                result = await _ytdlp_stream_download(request, platform)
                if result is not None:
                    return result
                if platform == "youtube":
                    raise HTTPException(status_code=502, detail="YouTube could not provide a complete download. Please retry.")

            return await _proxy_stream_download(request)
        finally:
            _active_downloads -= 1


async def _ytdlp_stream_download(request: DownloadRequest, platform: str) -> Optional[StreamingResponse]:
    temp_dir = tempfile.gettempdir()
    file_id = str(uuid.uuid4())
    filepath_base = os.path.join(temp_dir, file_id)

    if request.audio_only and platform == "youtube":
        ydl_opts = {
            "format": "140/bestaudio[ext=m4a]/bestaudio",
            "noplaylist": True,
            "outtmpl": filepath_base + ".%(ext)s",
            "quiet": True,
            "impersonate": ImpersonateTarget(client="chrome"),
            "concurrent_fragment_downloads": 8,
        }
    else:
        ydl_opts = {
            "format": _VIDEO_DOWNLOAD_FORMAT,
            "merge_output_format": "mp4",
            "noplaylist": True,
            "outtmpl": filepath_base + ".%(ext)s",
            "quiet": True,
            "impersonate": ImpersonateTarget(client="chrome"),
            "concurrent_fragment_downloads": 8,
        }
    ydl_opts = _apply_common_ydl_opts(ydl_opts)

    try:
        def download_video():
            variants = _youtube_public_ydl_variants(ydl_opts) if platform == "youtube" else [ydl_opts]
            last_err = None
            for variant in variants:
                try:
                    with SafeYoutubeDL(variant, single_video=platform == "youtube") as ydl:
                        ydl.download([request.original_url])
                    return
                except Exception as e:
                    last_err = e
                    if variant.get("impersonate") and _is_impersonation_unavailable_error(e):
                        _warn_impersonation_unavailable_once(e)
            if last_err:
                raise last_err

        await asyncio.to_thread(download_video)

        candidates = glob_module.glob(f"{filepath_base}.*")
        if not candidates:
            return None

        actual_file = candidates[0]
        ext = os.path.splitext(actual_file)[1].lstrip(".")
        media_type = "audio/mp4" if ext == "m4a" else ("audio/mpeg" if ext == "mp3" else "video/mp4")

        def iterfile():
            try:
                with open(actual_file, "rb") as f:
                    while chunk := f.read(64 * 1024):
                        yield chunk
            finally:
                for f in glob_module.glob(f"{filepath_base}.*"):
                    try:
                        os.remove(f)
                    except OSError:
                        pass

        return StreamingResponse(
            iterfile(),
            media_type=media_type,
            headers={
                "Content-Disposition": f"attachment; filename=media.{ext}",
                "Content-Length": str(os.path.getsize(actual_file)),
            },
        )
    except Exception as e:
        for f in glob_module.glob(f"{filepath_base}.*"):
            try:
                os.remove(f)
            except OSError:
                pass
        log.warning("yt-dlp download failed: %s", e)
    return None


class _ClosingStreamingResponse(StreamingResponse):
    def __init__(self, *args, resources: AsyncExitStack, **kwargs):
        super().__init__(*args, **kwargs)
        self._resources = resources

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self._resources.aclose()


async def _proxy_stream_download(request: DownloadRequest) -> StreamingResponse:
    resources = AsyncExitStack()
    try:
        session = await resources.enter_async_context(_new_async_session())
        response = await resources.enter_async_context(
            session.stream("GET", request.url, headers=request.headers, timeout=60)
        )
        if response.status_code not in (200, 206):
            raise HTTPException(status_code=502, detail=f"Media server returned {response.status_code}. Download unavailable.")
        is_image = any(ext in request.url.lower() for ext in (".jpg", ".jpeg", ".png", ".webp"))
        media_type = response.headers.get("Content-Type", "").partition(";")[0].strip().lower()
        if not media_type or media_type == "application/octet-stream":
            media_type = "image/jpeg" if is_image else "video/mp4"
        if not media_type.startswith(("image/", "video/", "audio/")):
            raise HTTPException(status_code=502, detail="Media server did not return a media file.")
        extensions = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp",
                      "image/gif": "gif", "image/avif": "avif", "audio/mp4": "m4a",
                      "audio/mpeg": "mp3", "video/webm": "webm", "video/quicktime": "mov"}
        file_ext = extensions.get(media_type, "jpg" if is_image else "mp4")
        headers = {"Content-Disposition": f"attachment; filename=media.{file_ext}"}
        length = response.headers.get("Content-Length", "")
        if length.isdigit() and not response.headers.get("Content-Encoding"):
            headers["Content-Length"] = length

        async def stream_generator():
            try:
                async for chunk in response.aiter_content():
                    yield chunk
            finally:
                with anyio.CancelScope(shield=True):
                    await resources.aclose()

        return _ClosingStreamingResponse(
            stream_generator(),
            resources=resources,
            media_type=media_type,
            headers=headers,
        )
    except BaseException as error:
        with anyio.CancelScope(shield=True):
            await resources.aclose()
        if isinstance(error, HTTPException) or not isinstance(error, Exception):
            raise
        raise HTTPException(status_code=502, detail="The media file could not be downloaded. Please retry.") from error


@app.get("/api/v1/download")
async def proxy_download_get(url: str, original_url: Optional[str] = None):
    try:
        req = DownloadRequest(url=url, original_url=original_url)
    except ValueError:
        raise HTTPException(status_code=400, detail="Enter a supported public media URL.") from None
    return await proxy_download(req)


@app.post("/api/v1/save", response_model=SaveResponse)
async def save_to_disk(request: SaveRequest):
    global _active_downloads
    platform = get_platform(request.original_url) if request.original_url else "unknown"
    ts = int(time.time() * 1000)
    is_image = any(ext in request.url.lower() for ext in (".jpg", ".jpeg", ".png", ".webp"))
    default_ext = "mp3" if request.audio_only else ("jpg" if is_image else "mp4")
    filename = request.filename or f"AnyGrab_{ts}.{default_ext}"
    filepath = safe_download_path(DOWNLOAD_DIR, filename)

    async with _download_semaphore:
        _active_downloads += 1
        try:
            if platform in ("instagram", "tiktok"):
                return await _save_via_proxy(request, filepath, filename)

            if request.original_url and not is_image:
                result = await _save_via_ytdlp(request, filepath, platform)
                if result:
                    return result
                if platform == "youtube":
                    raise HTTPException(status_code=502, detail="YouTube could not provide a complete download. Please retry.")

            return await _save_via_proxy(request, filepath, filename)
        finally:
            _active_downloads -= 1


async def _save_via_ytdlp(request: SaveRequest, filepath: Path, platform: str) -> Optional[SaveResponse]:
    if request.audio_only and platform == "youtube":
        ydl_opts = {
            "format": "140/bestaudio[ext=m4a]/bestaudio",
            "noplaylist": True,
            "outtmpl": str(filepath.with_suffix(".%(ext)s")),
            "quiet": True,
            "impersonate": ImpersonateTarget(client="chrome"),
            "concurrent_fragment_downloads": 8,
        }
    else:
        ydl_opts = {
            "format": _VIDEO_DOWNLOAD_FORMAT,
            "merge_output_format": "mp4",
            "noplaylist": True,
            "outtmpl": str(filepath.with_suffix(".%(ext)s")),
            "quiet": True,
            "impersonate": ImpersonateTarget(client="chrome"),
            "concurrent_fragment_downloads": 8,
        }
    ydl_opts = _apply_common_ydl_opts(ydl_opts)

    try:
        def dl():
            variants = _youtube_public_ydl_variants(ydl_opts) if platform == "youtube" else [ydl_opts]
            last_err = None
            for variant in variants:
                try:
                    with SafeYoutubeDL(variant, single_video=platform == "youtube") as ydl:
                        ydl.download([request.original_url])
                    return
                except Exception as e:
                    last_err = e
                    if variant.get("impersonate") and _is_impersonation_unavailable_error(e):
                        _warn_impersonation_unavailable_once(e)
            if last_err:
                raise last_err

        await asyncio.to_thread(dl)

        stem = filepath.with_suffix("").name
        saved = list(DOWNLOAD_DIR.glob(f"{stem}.*"))
        if saved and saved[0].stat().st_size > 0:
            actual = saved[0]
            return SaveResponse(success=True, filename=actual.name, path=str(actual), size=actual.stat().st_size)
    except Exception as e:
        log.warning("yt-dlp save failed: %s", e)
    return None


async def _save_via_proxy(request: SaveRequest, filepath: Path, filename: str) -> SaveResponse:
    try:
        async with _new_async_session() as session:
            resp = await session.get(request.url, headers=request.headers, timeout=60)
            if resp.status_code not in (200, 206):
                raise HTTPException(status_code=502, detail=f"Media server returned {resp.status_code}")
            filepath.write_bytes(resp.content)
            return SaveResponse(success=True, filename=filename, path=str(filepath), size=len(resp.content))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Save failed: {e}")


# ---------------------------------------------------------------------------
# Monitoring endpoints
# ---------------------------------------------------------------------------
@app.get("/api/v1/settings")
async def get_settings():
    return {
        "download_dir": str(DOWNLOAD_DIR),
    }


@app.get("/api/v1/health")
async def health():
    return {
        "status": "healthy",
        "active_extractions": _active_extractions,
        "active_downloads": _active_downloads,
        "max_extractions": MAX_CONCURRENT_EXTRACTIONS,
        "max_downloads": MAX_CONCURRENT_DOWNLOADS,
        "cache_size": _extract_cache.size,
        "total_requests": _total_requests,
    }

@app.get("/api/v1/queue")
async def queue_status():
    extract_available = MAX_CONCURRENT_EXTRACTIONS - _active_extractions
    download_available = MAX_CONCURRENT_DOWNLOADS - _active_downloads
    return {
        "extractions": {"active": _active_extractions, "max": MAX_CONCURRENT_EXTRACTIONS, "available": max(extract_available, 0)},
        "downloads": {"active": _active_downloads, "max": MAX_CONCURRENT_DOWNLOADS, "available": max(download_available, 0)},
        "cache": {"size": _extract_cache.size, "max": CACHE_MAX_SIZE, "ttl_seconds": CACHE_TTL_SECONDS},
    }

# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------
@app.get("/")
async def serve_frontend():
    return FileResponse(os.path.join("public", "index.html"))

app.mount("/", StaticFiles(directory="public"), name="public")

# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    try:
        port = int(os.getenv("PORT", "8000"))
    except ValueError:
        raise SystemExit("PORT must be an integer between 1 and 65535") from None
    if not 1 <= port <= 65535:
        raise SystemExit("PORT must be an integer between 1 and 65535")

    workers = int(os.getenv("WORKERS", "1"))
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        workers=workers,
        timeout_keep_alive=30,
        limit_concurrency=100,
        limit_max_requests=10000,
        access_log=False,
    )
