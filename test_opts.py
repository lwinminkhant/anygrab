import yt_dlp
ydl_opts = {
    'cookiesfrombrowser': ('brave',),
}
try:
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        print("Success initializing with cookiesfrombrowser")
except Exception as e:
    print(f"Error: {e}")
