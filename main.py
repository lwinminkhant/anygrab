import os
import re
import traceback
import http.cookiejar
import time
import tempfile
import uuid
import asyncio
import glob as glob_module
import hashlib
import logging
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import List, Optional, Dict, Any
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from curl_cffi.requests import AsyncSession, Session
from pydantic import BaseModel
import yt_dlp
from yt_dlp.networking.impersonate import ImpersonateTarget
from yt_dlp.cookies import extract_cookies_from_browser

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("anygrab")

# Accept either an absolute COOKIE_FILE path or a path relative to this repo.
_COOKIE_FILE_ENV = os.getenv("COOKIE_FILE", "cookies.txt")
_BASE_DIR = Path(__file__).resolve().parent
COOKIE_FILE_PATH = (Path(_COOKIE_FILE_ENV) if os.path.isabs(_COOKIE_FILE_ENV) else (_BASE_DIR / _COOKIE_FILE_ENV)).resolve()


def _netscape_cookie_file() -> Optional[str]:
    """Return cookies.txt path only if it is a non-empty regular file.

    Docker bind-mounts a missing source as a *directory* named cookies.txt, which must not be used.
    """
    p = COOKIE_FILE_PATH
    if not p.is_file() or p.stat().st_size == 0:
        return None
    return str(p)


def _allow_browser_cookies() -> bool:
    return os.getenv("ALLOW_BROWSER_COOKIES", "0").lower() in {"1", "true", "yes", "on"}

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
    log.info(
        "AnyGrab ready  |  extractions=%d  downloads=%d  cache=%ds/%d  rate=%d/%ds",
        MAX_CONCURRENT_EXTRACTIONS, MAX_CONCURRENT_DOWNLOADS,
        CACHE_TTL_SECONDS, CACHE_MAX_SIZE,
        RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW,
    )
    yield
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

_cors_origins = os.getenv("CORS_ORIGINS", "*").strip()
if _cors_origins == "*":
    _allow_origins = ["*"]
else:
    _allow_origins = [o.strip() for o in _cors_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allow_origins,
    allow_credentials=True,
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
            return await asyncio.wait_for(call_next(request), timeout=REQUEST_TIMEOUT)
        except asyncio.TimeoutError:
            return JSONResponse({"detail": "Request timed out."}, status_code=504)

    return await call_next(request)

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class URLRequest(BaseModel):
    url: str

class DownloadRequest(BaseModel):
    url: str
    headers: Dict[str, str] = {}
    original_url: Optional[str] = None
    audio_only: bool = False

class MediaResponse(BaseModel):
    platform: str
    caption: Optional[str] = None
    media_urls: List[str]
    metadata: Dict[str, Any]
    http_headers: Dict[str, str] = {}

class SaveRequest(BaseModel):
    url: str
    headers: Dict[str, str] = {}
    original_url: Optional[str] = None
    filename: Optional[str] = None
    audio_only: bool = False

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
    if "youtube.com" in url or "youtu.be" in url:
        return "youtube"
    if "tiktok.com" in url:
        return "tiktok"
    if "twitter.com" in url or "x.com" in url:
        return "x"
    if "facebook.com" in url or "fb.watch" in url:
        return "facebook"
    if "instagram.com" in url:
        return "instagram"
    return "unknown"

def _run_ytdlp_extract(url: str, ydl_opts: dict) -> dict:
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)

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

def extract_with_ytdlp(url: str, platform: str) -> MediaResponse:
    ydl_opts = {
        "skip_download": True,
        "format": "best[vcodec^=h264]/best[vcodec^=avc]/best",
        "impersonate": ImpersonateTarget(client="chrome"),
    }

    cf = _netscape_cookie_file()
    if cf:
        ydl_opts["cookiefile"] = cf
    elif platform != "youtube" and _allow_browser_cookies():
        ydl_opts["cookiesfrombrowser"] = ("brave",)

    try:
        info = _run_ytdlp_extract(url, ydl_opts)
    except Exception as first_err:
        if platform == "youtube":
            log.info("YouTube first attempt failed (%s), retrying without cookies…", first_err)
            retry_opts = {
                "skip_download": True,
                "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                "impersonate": ImpersonateTarget(client="chrome"),
            }
            try:
                info = _run_ytdlp_extract(url, retry_opts)
            except Exception as retry_err:
                log.error("YouTube retry failed: %s", traceback.format_exc())
                raise HTTPException(status_code=400, detail=f"Failed to extract youtube data: {retry_err}")
        elif platform == "tiktok":
            log.info("yt-dlp TikTok failed, trying fallback: %s", first_err)
            return extract_tiktok_fallback(url)
        else:
            log.error("Extraction error: %s", traceback.format_exc())
            raise HTTPException(status_code=400, detail=f"Failed to extract {platform} data: {first_err}")

    return _build_media_response(info, platform)

def extract_tiktok_fallback(url: str) -> MediaResponse:
    try:
        s = Session(impersonate="chrome")
        resp = s.get(f"https://www.tikwm.com/api/?url={url}&hd=1", timeout=15)
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
    s = Session(impersonate="chrome")
    p = _netscape_cookie_file()
    if p:
        try:
            cookie_jar = http.cookiejar.MozillaCookieJar(p)
            cookie_jar.load(ignore_discard=True, ignore_expires=True)
            for c in cookie_jar:
                s.cookies.set(c.name, c.value, domain=c.domain, path=c.path)
            return s
        except Exception as e:
            log.warning("cookies.txt could not be read (%s), trying browser cookies", e)
    if _allow_browser_cookies():
        try:
            jar = extract_cookies_from_browser("brave")
            for c in jar:
                if "instagram" in c.domain:
                    s.cookies.set(c.name, c.value, domain=c.domain, path=c.path)
        except Exception as e:
            log.warning("Could not extract browser cookies: %s", e)
    return s

def extract_instagram(url: str) -> MediaResponse:
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
            elif platform == "tiktok" and "/photo/" in request.url:
                result = await asyncio.to_thread(extract_tiktok_fallback, request.url)
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
            if platform == "tiktok" and request.original_url:
                try:
                    return await _download_tiktok_fallback(request.original_url)
                except Exception as e:
                    log.info("TikTok tikwm download failed: %s, trying yt-dlp…", e)

            if request.original_url:
                result = await _ytdlp_stream_download(request, platform)
                if result is not None:
                    return result

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
            "format": "bestvideo[ext=mp4][vcodec^=avc1]+140/best[vcodec^=h264]/best[vcodec^=avc]/best",
            "merge_output_format": "mp4",
            "noplaylist": True,
            "outtmpl": filepath_base + ".%(ext)s",
            "quiet": True,
            "impersonate": ImpersonateTarget(client="chrome"),
            "concurrent_fragment_downloads": 8,
        }

    cf = _netscape_cookie_file()
    if cf:
        ydl_opts["cookiefile"] = cf
    elif platform != "youtube" and _allow_browser_cookies():
        ydl_opts["cookiesfrombrowser"] = ("brave",)

    try:
        def download_video():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([request.original_url])

        await asyncio.to_thread(download_video)

        actual_files = glob_module.glob(filepath_base + ".*")
        actual_file = actual_files[0] if actual_files else None

        if actual_file and os.path.getsize(actual_file) > 0:
            ext = os.path.splitext(actual_file)[1].lstrip(".")
            mime_map = {
                "mp4": "video/mp4", "webm": "video/webm", "mp3": "audio/mpeg",
                "m4a": "audio/mp4", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                "png": "image/png", "webp": "image/webp",
            }
            media_type = mime_map.get(ext, "application/octet-stream")
            dl_filename = "audio" if request.audio_only else "media"
            file_size = os.path.getsize(actual_file)

            async def file_streamer():
                try:
                    with open(actual_file, "rb") as f:
                        while chunk := f.read(1024 * 256):
                            yield chunk
                finally:
                    _cleanup_files(filepath_base + ".*")

            return StreamingResponse(
                file_streamer(),
                media_type=media_type,
                headers={
                    "Content-Disposition": f"attachment; filename={dl_filename}.{ext}",
                    "Content-Length": str(file_size),
                },
            )
    except Exception as e:
        _cleanup_files(filepath_base + ".*")
        log.warning("yt-dlp download failed: %s", e)
    return None


async def _proxy_stream_download(request: DownloadRequest) -> StreamingResponse:
    try:
        async with AsyncSession(impersonate="chrome") as session:
            head_resp = await session.head(request.url, headers=request.headers, timeout=10)
            if head_resp.status_code not in (200, 206):
                raise HTTPException(status_code=502, detail=f"Media server returned {head_resp.status_code}. Download unavailable.")

        is_image = any(ext in request.url.lower() for ext in (".jpg", ".jpeg", ".png", ".webp"))
        media_type = "image/jpeg" if is_image else "video/mp4"
        file_ext = "jpg" if is_image else "mp4"

        async def stream_generator():
            async with AsyncSession(impersonate="chrome") as session:
                async with session.stream("GET", request.url, headers=request.headers) as response:
                    async for chunk in response.aiter_content():
                        yield chunk

        return StreamingResponse(
            stream_generator(),
            media_type=media_type,
            headers={"Content-Disposition": f"attachment; filename=media.{file_ext}"},
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Download failed: {e}")


async def _download_tiktok_fallback(tiktok_url: str) -> StreamingResponse:
    def fetch_api():
        s = Session(impersonate="chrome")
        api_resp = s.get(f"https://www.tikwm.com/api/?url={tiktok_url}&hd=1", timeout=15)
        api_resp.raise_for_status()
        return api_resp.json()

    data = await asyncio.to_thread(fetch_api)

    if data.get("code") != 0:
        raise ValueError(f"tikwm API error: {data.get('msg')}")

    video_url = data["data"].get("hdplay") or data["data"].get("play")
    if not video_url:
        raise ValueError("No video URL from tikwm")

    async def stream_generator():
        async with AsyncSession(impersonate="chrome") as session:
            async with session.stream("GET", video_url) as response:
                async for chunk in response.aiter_content():
                    yield chunk

    return StreamingResponse(
        stream_generator(),
        media_type="video/mp4",
        headers={"Content-Disposition": "attachment; filename=video.mp4"},
    )


@app.get("/api/v1/download")
async def proxy_download_get(url: str, original_url: Optional[str] = None):
    req = DownloadRequest(url=url, original_url=original_url)
    return await proxy_download(req)


@app.post("/api/v1/save", response_model=SaveResponse)
async def save_to_disk(request: SaveRequest):
    global _active_downloads
    platform = get_platform(request.original_url) if request.original_url else "unknown"
    ts = int(time.time() * 1000)
    is_image = any(ext in request.url.lower() for ext in (".jpg", ".jpeg", ".png", ".webp"))
    default_ext = "mp3" if request.audio_only else ("jpg" if is_image else "mp4")
    filename = request.filename or f"AnyGrab_{ts}.{default_ext}"
    filepath = DOWNLOAD_DIR / filename

    async with _download_semaphore:
        _active_downloads += 1
        try:
            if platform == "tiktok" and request.original_url and not is_image:
                try:
                    result = await _save_tiktok_fallback(request, filepath, filename)
                    if result:
                        return result
                except Exception as e:
                    log.info("TikTok tikwm save failed: %s, trying yt-dlp…", e)

            if request.original_url and not is_image:
                result = await _save_via_ytdlp(request, filepath, platform)
                if result:
                    return result

            return await _save_via_proxy(request, filepath, filename)
        finally:
            _active_downloads -= 1


async def _save_tiktok_fallback(request: SaveRequest, filepath: Path, filename: str) -> Optional[SaveResponse]:
    def fetch_api():
        s = Session(impersonate="chrome")
        api_resp = s.get(f"https://www.tikwm.com/api/?url={request.original_url}&hd=1", timeout=15)
        api_resp.raise_for_status()
        return api_resp.json()

    data = await asyncio.to_thread(fetch_api)
    if data.get("code") != 0:
        return None

    video_url = data["data"].get("hdplay") or data["data"].get("play")
    if not video_url:
        return None

    async with AsyncSession(impersonate="chrome") as session:
        resp = await session.get(video_url, timeout=60)
        filepath.write_bytes(resp.content)
        return SaveResponse(success=True, filename=filename, path=str(filepath), size=len(resp.content))


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
            "format": "bestvideo[ext=mp4][vcodec^=avc1]+140/best[vcodec^=h264]/best[vcodec^=avc]/best",
            "merge_output_format": "mp4",
            "noplaylist": True,
            "outtmpl": str(filepath.with_suffix(".%(ext)s")),
            "quiet": True,
            "impersonate": ImpersonateTarget(client="chrome"),
            "concurrent_fragment_downloads": 8,
        }

    cf = _netscape_cookie_file()
    if cf:
        ydl_opts["cookiefile"] = cf
    elif platform != "youtube" and _allow_browser_cookies():
        ydl_opts["cookiesfrombrowser"] = ("brave",)

    try:
        def dl():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([request.original_url])

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
        async with AsyncSession(impersonate="chrome") as session:
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
        "cookie_file": str(COOKIE_FILE_PATH),
        "cookies_present": _netscape_cookie_file() is not None,
    }


@app.post("/api/v1/cookies")
async def upload_cookies(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file uploaded")
    try:
        content = await file.read()
    finally:
        await file.close()

    if not content:
        raise HTTPException(status_code=400, detail="Empty file")

    COOKIE_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    COOKIE_FILE_PATH.write_bytes(content)
    if not _netscape_cookie_file():
        raise HTTPException(status_code=400, detail="Invalid cookies file")

    return {"ok": True, "path": str(COOKIE_FILE_PATH)}

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

    workers = int(os.getenv("WORKERS", "1"))
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        workers=workers,
        timeout_keep_alive=30,
        limit_concurrency=100,
        limit_max_requests=10000,
        access_log=False,
    )
