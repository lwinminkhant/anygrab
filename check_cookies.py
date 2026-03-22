import yt_dlp
import sys

browsers = ['safari', 'firefox']
success = None

for browser in browsers:
    opts = {'cookiesfrombrowser': (browser,), 'quiet': True, 'skip_download': True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info("https://www.tiktok.com/@sarsarpan0/video/7616995509441465620", download=False)
        print(f"SUCCESS: {browser}")
        success = browser
        break
    except Exception as e:
        print(f"FAILED: {browser} - {str(e)[:50]}")

if success:
    sys.exit(0)
else:
    sys.exit(1)
