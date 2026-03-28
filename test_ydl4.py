import yt_dlp
from yt_dlp.networking.impersonate import ImpersonateTarget

ydl_opts = {
    'skip_download': True,
    'cookiesfrombrowser': ('brave',),
    'impersonate': ImpersonateTarget(client='chrome'),
}
with yt_dlp.YoutubeDL(ydl_opts) as ydl:
    info = ydl.extract_info("https://youtu.be/f2xGxd9xPYA?si=eeJufZnpJSxQxQgk", download=False)
    print("Success:", info.get('title'))
