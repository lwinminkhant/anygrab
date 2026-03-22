import yt_dlp
import os

ydl_opts = {
    'format': 'best',
    'outtmpl': 'test_video.mp4',
    'cookiesfrombrowser': ('brave',),
    'quiet': False
}
url = "https://www.tiktok.com/@aung.kyaw.kyaw175/video/7602848669947219220"
with yt_dlp.YoutubeDL(ydl_opts) as ydl:
    ydl.download([url])

print("File exists:", os.path.exists('test_video.mp4'))
