from main import extract_with_ytdlp

try:
    url = "https://youtu.be/f2xGxd9xPYA?si=eeJufZnpJSxQxQgk"
    print("Extracting...")
    info = extract_with_ytdlp(url, "youtube")
    print("Success!")
    print("Caption:", info.caption)
    print("URLs:", info.media_urls)
except Exception as e:
    print("Failed:", e)
