"""Offline regressions for public YouTube clients and adaptive-only media."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import shutil
import socket
import subprocess
from unittest.mock import AsyncMock

import pytest
import yt_dlp
from fastapi.responses import Response
from fastapi.testclient import TestClient
from yt_dlp.extractor.common import InfoExtractor

import main
import media_source
from public_media_safety import SafeYoutubeDL


VIDEO_ID = "esrPUSvaDNE"
VIDEO_URL = "https://rr1.googlevideo.com/video-only.mp4"
AUDIO_URL = "https://rr1.googlevideo.com/audio-only.m4a"
COMBINED_URL = "https://rr1.googlevideo.com/combined.mp4"
ADAPTIVE_FORMATS = [
    {
        "format_id": "140", "url": AUDIO_URL, "ext": "m4a",
        "acodec": "mp4a.40.2", "vcodec": "none", "abr": 128,
    },
    {
        "format_id": "137", "url": VIDEO_URL, "ext": "mp4",
        "vcodec": "avc1.640028", "acodec": "none", "height": 1080,
        "width": 1920, "fps": 30,
    },
]
VP9_URL = "https://rr1.googlevideo.com/video-only.webm"
VP9_FORMATS = [
    {
        "format_id": "251", "url": "https://rr1.googlevideo.com/audio-only.webm",
        "ext": "webm", "acodec": "opus", "vcodec": "none", "abr": 128,
    },
    {
        "format_id": "248", "url": VP9_URL, "ext": "webm", "vcodec": "vp9",
        "acodec": "none", "height": 1080, "width": 1920, "fps": 30,
    },
]


@pytest.fixture
def offline_youtube(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
    ])
    monkeypatch.setattr(main, "_has_valid_cookies", lambda: False)
    monkeypatch.setattr(main, "PROXY_URL", None)

    def install(formats, *, account=False):
        processed_formats = []

        class FixtureYoutubeIE(InfoExtractor):
            _VALID_URL = r"https://www\.youtube\.com/watch\?v=.+"

            def _real_extract(self, url):
                return {
                    "id": VIDEO_ID, "title": "Adaptive fixture", "duration": 15,
                    "formats": deepcopy(formats),
                }

        class FixtureYoutubeDL(SafeYoutubeDL):
            def add_default_info_extractors(self):
                self.add_info_extractor(FixtureYoutubeIE())

            def urlopen(self, request):
                raise AssertionError("Regression tests must never access the network")

            def process_info(self, info_dict):
                selected_formats = info_dict.get("requested_formats", [info_dict])
                processed_formats.append([item["format_id"] for item in selected_formats])
                if account:
                    Path(self.prepare_filename(info_dict)).write_bytes(b"fixture merged media")

        monkeypatch.setattr(main, "SafeYoutubeDL", FixtureYoutubeDL)
        if account:
            monkeypatch.setattr(media_source.yt_dlp, "YoutubeDL", FixtureYoutubeDL)
        return processed_formats

    return install


def test_adaptive_only_youtube_video_still_has_a_preview(offline_youtube):
    offline_youtube(ADAPTIVE_FORMATS)
    response = main.extract_with_ytdlp(f"https://www.youtube.com/watch?v={VIDEO_ID}", "youtube")
    assert response.media_urls == [VIDEO_URL]
    assert response.metadata["id"] == VIDEO_ID
    assert response.metadata["duration"] == 15


def test_adaptive_only_vp9_video_still_has_a_preview(offline_youtube):
    offline_youtube(VP9_FORMATS)
    response = main.extract_with_ytdlp(f"https://www.youtube.com/watch?v={VIDEO_ID}", "youtube")
    assert response.media_urls == [VP9_URL]
    assert response.metadata["id"] == VIDEO_ID


@pytest.mark.parametrize("operation", ["stream", "save"])
@pytest.mark.parametrize(("formats", "expected_ids"), [
    ([{**ADAPTIVE_FORMATS[0], "format_id": "141"}, ADAPTIVE_FORMATS[1]], ["137", "141"]),
    (VP9_FORMATS, ["248", "251"]),
], ids=["avc-without-audio-140", "vp9-opus"])
def test_video_download_helpers_select_both_available_adaptive_tracks(
    formats, expected_ids, operation, offline_youtube, monkeypatch, tmp_path,
):
    processed_formats = offline_youtube(formats)
    monkeypatch.setattr(main, "DOWNLOAD_DIR", tmp_path)
    monkeypatch.setattr(main.tempfile, "gettempdir", lambda: str(tmp_path))
    request = main.SaveRequest(
        url=VIDEO_URL, original_url=f"https://www.youtube.com/watch?v={VIDEO_ID}",
        filename="fixture.mp4",
    )
    if operation == "stream":
        result = asyncio.run(main._ytdlp_stream_download(request, "youtube"))
    else:
        result = asyncio.run(main._save_via_ytdlp(request, tmp_path / "fixture.mp4", "youtube"))
    assert processed_formats == [expected_ids]
    assert result is None  # Actual media writes are replaced by process_info above.
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("endpoint", ["download", "save"])
def test_youtube_download_failure_does_not_save_silent_preview(
    endpoint, offline_youtube, monkeypatch, tmp_path,
):
    monkeypatch.setattr(main, "DOWNLOAD_DIR", tmp_path)
    monkeypatch.setattr(main.RateLimiter, "is_allowed", lambda *args: True)
    download = AsyncMock(return_value=None)
    save = AsyncMock(return_value=None)
    proxy_download = AsyncMock(return_value=Response(content=b"silent preview"))
    proxy_save = AsyncMock(return_value=main.SaveResponse(
        success=True, filename="fixture.mp4", path=str(tmp_path / "fixture.mp4"), size=0,
    ))
    monkeypatch.setattr(main, "_ytdlp_stream_download", download)
    monkeypatch.setattr(main, "_save_via_ytdlp", save)
    monkeypatch.setattr(main, "_proxy_stream_download", proxy_download)
    monkeypatch.setattr(main, "_save_via_proxy", proxy_save)
    payload = {"url": VIDEO_URL, "original_url": f"https://www.youtube.com/watch?v={VIDEO_ID}"}
    with TestClient(main.app, base_url="http://localhost") as client:
        response = client.post(f"/api/v1/{endpoint}", json=payload)
    assert response.status_code >= 400, response.text
    proxy_download.assert_not_awaited()
    proxy_save.assert_not_awaited()


@pytest.mark.parametrize(("formats", "expected_ids"), [
    ([{**ADAPTIVE_FORMATS[0], "format_id": "141"}, ADAPTIVE_FORMATS[1]], ["137", "141"]),
    (VP9_FORMATS, ["248", "251"]),
], ids=["avc-without-audio-140", "vp9-opus"])
def test_account_source_download_selects_both_adaptive_tracks(formats, expected_ids, offline_youtube):
    processed_formats = offline_youtube(formats, account=True)
    with media_source.downloaded_source(f"https://www.youtube.com/watch?v={VIDEO_ID}") as path:
        assert path.suffix == ".mp4"
        assert path.read_bytes() == b"fixture merged media"
        assert processed_formats == [expected_ids]


def _generate_local_adaptive_tracks(ffmpeg, directory):
    video = directory / "video.mp4"
    audio = directory / "audio.m4a"
    common = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i"]
    subprocess.run([
        *common, "color=c=blue:s=64x64:r=10", "-t", "0.2", "-c:v", "libx264",
        "-pix_fmt", "yuv420p", "-an", str(video),
    ], check=True, capture_output=True)
    subprocess.run([
        *common, "sine=frequency=1000:sample_rate=44100", "-t", "0.2", "-c:a", "aac",
        "-vn", str(audio),
    ], check=True, capture_output=True)
    return [
        {**ADAPTIVE_FORMATS[0], "format_id": "141", "url": audio.as_uri()},
        {**ADAPTIVE_FORMATS[1], "url": video.as_uri()},
    ]


def test_account_source_really_merges_adaptive_video_and_audio(monkeypatch, tmp_path):
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("ffmpeg and ffprobe are required for the actual merge integration")
    formats = _generate_local_adaptive_tracks(ffmpeg, tmp_path)

    class LocalYoutubeIE(InfoExtractor):
        _VALID_URL = r"https://www\.youtube\.com/watch\?v=.+"

        def _real_extract(self, url):
            return {"id": VIDEO_ID, "title": "Local merge fixture", "formats": deepcopy(formats)}

    class LocalYoutubeDL(yt_dlp.YoutubeDL):
        def __init__(self, options):
            super().__init__({**options, "enable_file_urls": True})

        def add_default_info_extractors(self):
            self.add_info_extractor(LocalYoutubeIE())

        def urlopen(self, request):
            request_url = request if isinstance(request, str) else request.url
            assert request_url.startswith("file:"), "Merge integration must never access the network"
            return super().urlopen(request)

    monkeypatch.setattr(media_source.yt_dlp, "YoutubeDL", LocalYoutubeDL)
    with media_source.downloaded_source(f"https://www.youtube.com/watch?v={VIDEO_ID}") as path:
        assert path.suffix == ".mp4"
        probe = subprocess.run([
            ffprobe, "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", str(path),
        ], check=True, capture_output=True, text=True)
        assert {stream["codec_type"] for stream in json.loads(probe.stdout)["streams"]} == {"video", "audio"}


def test_combined_format_remains_preferred_for_preview(offline_youtube):
    combined = {
        "format_id": "18", "url": COMBINED_URL, "ext": "mp4", "height": 360,
        "vcodec": "avc1.42001E", "acodec": "mp4a.40.2", "width": 640,
    }
    offline_youtube([*ADAPTIVE_FORMATS, combined])
    response = main.extract_with_ytdlp(f"https://www.youtube.com/watch?v={VIDEO_ID}", "youtube")
    assert response.media_urls == [COMBINED_URL]
    assert response.metadata["id"] == VIDEO_ID


def test_public_client_fallback_extends_defaults_without_deprecated_client():
    base_options = {
        "quiet": True,
        "extractor_args": {
            "youtube": {"player_skip": ["configs"]},
            "tiktok": {"api_hostname": ["example.invalid"]},
        },
    }
    original_options = deepcopy(base_options)
    variants = main._youtube_public_ydl_variants(base_options)
    assert variants[0] == original_options
    assert base_options == original_options
    fallback = variants[-1]
    clients = fallback["extractor_args"]["youtube"]["player_client"]
    assert "default" in clients
    assert "web_safari" in clients
    assert "android_vr" not in clients
    assert fallback["extractor_args"]["youtube"]["player_skip"] == ["configs"]
    assert fallback["extractor_args"]["tiktok"] == original_options["extractor_args"]["tiktok"]
    assert fallback["quiet"] is True
