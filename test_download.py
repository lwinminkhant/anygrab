import asyncio
from curl_cffi.requests import AsyncSession
import yt_dlp
import json

async def main():
    ydl_opts = {
        'format': 'best',
        'noplaylist': True,
        'cookiesfrombrowser': ('brave',),
        'quiet': True,
    }
    url = "https://www.tiktok.com/@aung.kyaw.kyaw175/video/7602848669947219220"
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        video_url = info.get('url')
        headers = info.get('http_headers', {})
        print(f"Extracted URL: {video_url[:100]}...")
        print(f"Headers: {json.dumps(headers, indent=2)}")

    async with AsyncSession(impersonate="chrome") as session:
        print("Starting stream...")
        async with session.stream("GET", video_url, headers=headers) as response:
            print(f"Response status: {response.status_code}")
            content_length = response.headers.get('content-length')
            print(f"Content-Length: {content_length}")
            
            size = 0
            with open("test.mp4", "wb") as f:
                async for chunk in response.aiter_content():
                    f.write(chunk)
                    size += len(chunk)
            print(f"Downloaded {size} bytes")

asyncio.run(main())
