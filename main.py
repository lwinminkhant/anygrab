import os
import re
import traceback
import http.cookiejar
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse, FileResponse
from curl_cffi.requests import AsyncSession, Session
from pydantic import BaseModel
import yt_dlp
from yt_dlp.networking.impersonate import ImpersonateTarget
import instaloader
import tempfile
import uuid
import asyncio
import glob as glob_module
from typing import List, Optional, Dict, Any

app = FastAPI(
    title="Universal Social Media Downloader API",
    description="API to extract metadata, captions, and media URLs from various social platforms."
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ig_loader = instaloader.Instaloader(quiet=True)

class URLRequest(BaseModel):
    url: str

class DownloadRequest(BaseModel):
    url: str
    headers: Dict[str, str] = {}
    original_url: Optional[str] = None

class MediaResponse(BaseModel):
    platform: str
    caption: Optional[str] = None
    media_urls: List[str]
    metadata: Dict[str, Any]
    http_headers: Dict[str, str] = {}

def get_platform(url: str) -> str:
    if "youtube.com" in url or "youtu.be" in url: return "youtube"
    if "tiktok.com" in url: return "tiktok"
    if "twitter.com" in url or "x.com" in url: return "x"
    if "facebook.com" in url or "fb.watch" in url: return "facebook"
    if "instagram.com" in url: return "instagram"
    return "unknown"

def extract_with_ytdlp(url: str, platform: str) -> MediaResponse:
    ydl_opts = {
        'skip_download': True,
        'format': 'best[vcodec^=h264]/best[vcodec^=avc]/best',
        'impersonate': ImpersonateTarget(client='chrome'),
    }
    
    # Try to use cookies.txt if it exists for authentication
    if os.path.exists('cookies.txt'):
        ydl_opts['cookiefile'] = 'cookies.txt'
    else:
        # Automagically use cookies from the user's Brave browser
        ydl_opts['cookiesfrombrowser'] = ('brave',)
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            caption = info.get('description') or info.get('title')
            media_urls = []
            if 'url' in info:
                media_urls.append(info['url'])
            elif 'entries' in info:
                for entry in info['entries']:
                    if 'url' in entry:
                        media_urls.append(entry['url'])
            
            clean_metadata = {
                "id": info.get("id"),
                "uploader": info.get("uploader"),
                "upload_date": info.get("upload_date"),
                "view_count": info.get("view_count"),
                "like_count": info.get("like_count"),
                "duration": info.get("duration"),
                "thumbnail": info.get("thumbnail")
            }

            return MediaResponse(
                platform=platform,
                caption=caption,
                media_urls=media_urls,
                metadata=clean_metadata,
                http_headers=info.get('http_headers', {})
            )
            
    except Exception as e:
        if platform == "tiktok":
            print(f"yt-dlp TikTok extraction failed, trying fallback: {e}")
            return extract_tiktok_fallback(url)
        err_msg = traceback.format_exc()
        print("EXTRACTION ERROR:", err_msg)
        raise HTTPException(status_code=400, detail=f"Failed to extract {platform} data: {str(e)}\n\n{err_msg}")

def extract_tiktok_fallback(url: str) -> MediaResponse:
    """Fallback TikTok extractor using tikwm.com API when yt-dlp gets 403'd."""
    try:
        s = Session(impersonate="chrome")
        resp = s.get(f"https://www.tikwm.com/api/?url={url}&hd=1", timeout=15)
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 0:
            raise ValueError(f"tikwm API error: {data.get('msg', 'unknown')}")

        d = data["data"]
        media_urls = []
        if d.get("hdplay"):
            media_urls.append(d["hdplay"])
        if d.get("play"):
            media_urls.append(d["play"])
        if not media_urls:
            raise ValueError("No video URLs returned from tikwm API")

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
                "duration": d.get("duration"),
                "thumbnail": d.get("cover"),
            },
            http_headers={},
        )
    except HTTPException:
        raise
    except Exception as e:
        err_msg = traceback.format_exc()
        print("TIKTOK FALLBACK ERROR:", err_msg)
        raise HTTPException(status_code=400, detail=f"Failed to extract TikTok data: {str(e)}")

def extract_instagram(url: str) -> MediaResponse:
    try:
        if os.path.exists("cookies.txt"):
            cookie_jar = http.cookiejar.MozillaCookieJar("cookies.txt")
            cookie_jar.load(ignore_discard=True, ignore_expires=True)
            ig_loader.context._session.cookies.update(cookie_jar)

        match = re.search(r"(?:p|reel|tv)/([^/?#&]+)", url)
        if not match:
            raise ValueError("Invalid Instagram URL. Must be a post or reel.")
        
        shortcode = match.group(1)
        post = instaloader.Post.from_shortcode(ig_loader.context, shortcode)
        
        media_urls = []
        if post.is_video:
            media_urls.append(post.video_url)
        else:
            media_urls.append(post.url)
            
        if post.typename == 'GraphSidecar':
            media_urls = []
            for node in post.get_sidecar_nodes():
                if node.is_video:
                    media_urls.append(node.video_url)
                else:
                    media_urls.append(node.display_url)

        metadata = {
            "id": post.shortcode,
            "owner_username": post.owner_username,
            "date_utc": str(post.date_utc),
            "likes": post.likes,
            "comments": post.comments,
            "is_video": post.is_video
        }

        return MediaResponse(
            platform="instagram",
            caption=post.caption,
            media_urls=media_urls,
            metadata=metadata
        )

    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to extract Instagram data: {str(e)}")

@app.post("/api/v1/download")
async def proxy_download(request: DownloadRequest):
    """Proxy the video stream to bypass CORS and Varnish Cache 403 blocks."""
    platform = get_platform(request.original_url) if request.original_url else "unknown"

    # TikTok: use tikwm (fast, reliable) first, yt-dlp as fallback
    if platform == "tiktok" and request.original_url:
        try:
            return await _download_tiktok_fallback(request.original_url)
        except Exception as e:
            print(f"TikTok tikwm download failed: {e}, trying yt-dlp...")

    # yt-dlp file download (works for most platforms, sometimes TikTok)
    if request.original_url:
        temp_dir = tempfile.gettempdir()
        file_id = str(uuid.uuid4())
        filepath_base = os.path.join(temp_dir, file_id)

        ydl_opts = {
            'format': 'best[vcodec^=h264]/best[vcodec^=avc]/best',
            'merge_output_format': 'mp4',
            'noplaylist': True,
            'outtmpl': filepath_base + '.%(ext)s',
            'quiet': True,
            'impersonate': ImpersonateTarget(client='chrome'),
        }

        if os.path.exists('cookies.txt'):
            ydl_opts['cookiefile'] = 'cookies.txt'
        else:
            ydl_opts['cookiesfrombrowser'] = ('brave',)

        try:
            def download_video():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([request.original_url])

            await asyncio.to_thread(download_video)

            actual_files = glob_module.glob(filepath_base + '.*')
            actual_file = actual_files[0] if actual_files else None

            if actual_file and os.path.getsize(actual_file) > 0:
                ext = os.path.splitext(actual_file)[1].lstrip('.')
                mime_map = {'mp4': 'video/mp4', 'webm': 'video/webm', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png', 'webp': 'image/webp'}
                media_type = mime_map.get(ext, 'application/octet-stream')

                async def file_streamer():
                    try:
                        with open(actual_file, "rb") as f:
                            while chunk := f.read(1024 * 64):
                                yield chunk
                    finally:
                        for cleanup_f in glob_module.glob(filepath_base + '.*'):
                            try:
                                os.remove(cleanup_f)
                            except:
                                pass

                return StreamingResponse(
                    file_streamer(),
                    media_type=media_type,
                    headers={"Content-Disposition": f"attachment; filename=media.{ext}"},
                )
        except Exception as e:
            print(f"yt-dlp download failed: {e}")

    # Last resort: proxy the direct media URL (works for non-TikTok CDNs)
    try:
        async with AsyncSession(impersonate="chrome") as session:
            head_resp = await session.head(request.url, headers=request.headers)
            if head_resp.status_code not in (200, 206):
                raise HTTPException(
                    status_code=502,
                    detail=f"Media server returned {head_resp.status_code}. Download unavailable.",
                )

        is_image = any(ext in request.url.lower() for ext in ('.jpg', '.jpeg', '.png', '.webp'))
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
        raise HTTPException(status_code=500, detail=f"Download failed: {str(e)}")

async def _download_tiktok_fallback(tiktok_url: str) -> StreamingResponse:
    """Download a TikTok video via tikwm.com when yt-dlp is blocked."""
    s = Session(impersonate="chrome")
    api_resp = s.get(f"https://www.tikwm.com/api/?url={tiktok_url}&hd=1", timeout=15)
    api_resp.raise_for_status()
    data = api_resp.json()

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

@app.post("/api/v1/extract", response_model=MediaResponse)
async def extract_social_media(request: URLRequest):
    platform = get_platform(request.url)
    if platform == "unknown":
        raise HTTPException(status_code=400, detail="Unsupported platform or invalid URL")
    if platform == "instagram":
        try:
            return extract_with_ytdlp(request.url, platform)
        except Exception as e:
            print(f"yt-dlp Instagram extraction failed, trying instaloader fallback: {e}")
            return extract_instagram(request.url)
    else:
        return extract_with_ytdlp(request.url, platform)

@app.get("/")
async def serve_frontend():
    """Serve the frontend index.html on the root URL."""
    return FileResponse(os.path.join("public", "index.html"))

# Mount static files for assets (css, js) directly on the root path
app.mount("/", StaticFiles(directory="public"), name="public")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
