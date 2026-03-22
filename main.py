import os
import re
import traceback
import http.cookiejar
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import yt_dlp
from yt_dlp.networking.impersonate import ImpersonateTarget
import instaloader
from typing import List, Optional, Dict, Any

app = FastAPI(
    title="Universal Social Media Downloader API",
    description="API to extract metadata, captions, and media URLs from various social platforms."
)

ig_loader = instaloader.Instaloader(quiet=True)

class URLRequest(BaseModel):
    url: str

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
        'format': 'best',
        'impersonate': ImpersonateTarget(client='chrome'),
    }
    
    if os.path.exists("cookies.txt"):
        ydl_opts['cookiefile'] = 'cookies.txt'
    
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
        err_msg = traceback.format_exc()
        print("EXTRACTION ERROR:", err_msg)
        raise HTTPException(status_code=400, detail=f"Failed to extract {platform} data: {str(e)}\n\n{err_msg}")

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

@app.post("/api/v1/extract", response_model=MediaResponse)
async def extract_social_media(request: URLRequest):
    platform = get_platform(request.url)
    if platform == "unknown":
        raise HTTPException(status_code=400, detail="Unsupported platform or invalid URL")
    if platform == "instagram":
        return extract_instagram(request.url)
    else:
        return extract_with_ytdlp(request.url, platform)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
