"""Exercise public API delivery using real yt-dlp downloads and FFmpeg merging."""
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import shutil
import socket
import subprocess

import pytest
from fastapi.testclient import TestClient
from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.networking.common import Response

import main
from public_media_safety import SafeYoutubeDL, validate_headers, validate_url


SOURCE_URL = "https://www.youtube.com/watch?v=esrPUSvaDNE"
VIDEO_URL = "https://rr1.googlevideo.com/delivery-video.mp4"
AUDIO_URL = "https://rr1.googlevideo.com/delivery-audio.m4a"


@pytest.fixture
def real_download_fixture(monkeypatch, tmp_path):
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg and ffprobe are required for real download delivery")
    source_dir = tmp_path / "source"
    output_dir = tmp_path / "output"
    source_dir.mkdir()
    output_dir.mkdir()
    video = source_dir / "video.mp4"
    audio = source_dir / "audio.m4a"
    common = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i"]
    subprocess.run([
        *common, "color=c=blue:s=64x64:r=10", "-t", "0.3", "-c:v", "libx264",
        "-pix_fmt", "yuv420p", "-an", str(video),
    ], check=True, capture_output=True)
    subprocess.run([
        *common, "sine=frequency=1000:sample_rate=44100", "-t", "0.3",
        "-c:a", "aac", "-vn", str(audio),
    ], check=True, capture_output=True)
    bodies = {VIDEO_URL: video.read_bytes(), AUDIO_URL: audio.read_bytes()}
    formats = [
        {"format_id": "137", "url": VIDEO_URL, "ext": "mp4", "height": 64,
         "vcodec": "avc1.640028", "acodec": "none"},
        {"format_id": "140", "url": AUDIO_URL, "ext": "m4a", "abr": 128,
         "vcodec": "none", "acodec": "mp4a.40.2"},
    ]
    requested_urls = []
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
    ])

    class FixtureYoutubeIE(InfoExtractor):
        _VALID_URL = r"https://www\.youtube\.com/watch\?v=.+"

        def _real_extract(self, url):
            assert url == SOURCE_URL
            return {"id": "esrPUSvaDNE", "title": "Delivery fixture", "formats": deepcopy(formats)}

    class FixtureYoutubeDL(SafeYoutubeDL):
        def add_default_info_extractors(self):
            self.add_info_extractor(FixtureYoutubeIE())

        def urlopen(self, request):
            url = request if isinstance(request, str) else request.url
            validate_url(url)
            if not isinstance(request, str):
                validate_headers(dict(request.headers))
            assert url in bodies, f"Delivery integration must never access the network: {url}"
            requested_urls.append(url)
            body = bodies[url]
            return Response(BytesIO(body), url, {"Content-Length": str(len(body))})

    monkeypatch.setattr(main, "SafeYoutubeDL", FixtureYoutubeDL)
    monkeypatch.setattr(main, "_has_valid_cookies", lambda: False)
    monkeypatch.setattr(main, "PROXY_URL", None)
    monkeypatch.setattr(main, "DOWNLOAD_DIR", output_dir)
    monkeypatch.setattr(main.tempfile, "gettempdir", lambda: str(output_dir))
    monkeypatch.setattr(main.RateLimiter, "is_allowed", lambda *args: True)
    return output_dir, requested_urls, ffprobe


def _probe_streams(ffprobe, path):
    probe = subprocess.run([
        ffprobe, "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", str(path),
    ], check=True, capture_output=True, text=True)
    return {stream["codec_type"] for stream in json.loads(probe.stdout)["streams"]}


@pytest.mark.parametrize(("method", "audio_only"), [
    ("post", False), ("post", True), ("get", False),
], ids=["post-merged-video", "post-audio", "get-merged-video"])
def test_download_api_delivers_real_media_and_cleans_temporary_files(
    audio_only, method, real_download_fixture, tmp_path,
):
    output_dir, requested_urls, ffprobe = real_download_fixture
    payload = {"url": VIDEO_URL, "original_url": SOURCE_URL, "audio_only": audio_only}
    with TestClient(main.app, base_url="http://localhost") as client:
        response = (client.post("/api/v1/download", json=payload) if method == "post"
                    else client.get("/api/v1/download", params=payload))
    assert response.status_code == 200, response.text
    assert int(response.headers["content-length"]) == len(response.content) > 1000
    extension = "m4a" if audio_only else "mp4"
    assert response.headers["content-disposition"] == f"attachment; filename=media.{extension}"
    assert response.headers["content-type"] == ("audio/mp4" if audio_only else "video/mp4")
    delivered = tmp_path / f"delivered.{extension}"
    delivered.write_bytes(response.content)
    assert _probe_streams(ffprobe, delivered) == ({"audio"} if audio_only else {"video", "audio"})
    assert set(requested_urls) == ({AUDIO_URL} if audio_only else {VIDEO_URL, AUDIO_URL})
    assert list(output_dir.iterdir()) == []


def test_save_api_writes_real_merged_media(real_download_fixture):
    output_dir, requested_urls, ffprobe = real_download_fixture
    with TestClient(main.app, base_url="http://localhost") as client:
        response = client.post("/api/v1/save", json={
            "url": VIDEO_URL, "original_url": SOURCE_URL, "filename": "delivery.mp4",
        })
    assert response.status_code == 200, response.text
    result = response.json()
    saved = Path(result["path"])
    assert result["success"] is True
    assert result["filename"] == "delivery.mp4"
    assert saved == output_dir / "delivery.mp4"
    assert result["size"] == saved.stat().st_size > 1000
    assert _probe_streams(ffprobe, saved) == {"video", "audio"}
    assert set(requested_urls) == {VIDEO_URL, AUDIO_URL}
    assert list(output_dir.iterdir()) == [saved]
