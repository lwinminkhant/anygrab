FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
# ffmpeg: yt-dlp / media. build-essential: tgcrypto (Pyrogram) compiles C — slim has no compiler.
# Keep install + pip + purge in one RUN so compiler packages are not baked into a lower layer.
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg build-essential && \
    pip install --no-cache-dir -r requirements.txt && \
    apt-get purge -y build-essential && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

COPY . .
# Optional cookies are not copied (.dockerignore); empty file avoids Docker bind creating a directory.
RUN touch cookies.txt

EXPOSE 8000

CMD ["python", "main.py"]
