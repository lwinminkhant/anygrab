"""Instagram photo extraction and selected carousel-item download regressions."""
from contextlib import asynccontextmanager
from copy import deepcopy
import asyncio
import socket

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect
from yt_dlp.extractor.instagram import InstagramIE

import main


POST_URL = "https://www.instagram.com/p/DefCkhRikbG/?dlrf=NTc4MTIwNjQ2YQ=="
PHOTO_URLS = [f"https://scontent.cdninstagram.com/photo-{index}.jpg" for index in range(6)]
VIDEO_URL = "https://scontent.cdninstagram.com/video.mp4"


def _photo(index):
    return {
        "pk": str(10000 + index), "media_type": 1,
        "image_versions2": {"candidates": [
            {"url": PHOTO_URLS[index], "width": 1080, "height": 1350},
            {"url": f"https://scontent.cdninstagram.com/thumb-{index}.jpg", "width": 150, "height": 150},
        ]},
    }


def _video():
    return {
        "pk": "20000", "media_type": 2, "video_codec": "avc1.640028", "has_audio": True,
        "video_versions": [{"url": VIDEO_URL, "width": 1080, "height": 1920, "type": 101}],
        "image_versions2": {"candidates": [
            {"url": "https://scontent.cdninstagram.com/video-cover.jpg", "width": 1080, "height": 1920},
        ]},
    }


def _photo_without_dimensions(index):
    return {**_photo(index), "image_versions2": {"candidates": [
        {"url": PHOTO_URLS[index]},
        {"url": f"https://scontent.cdninstagram.com/thumb-{index}.jpg"},
    ]}}


@pytest.fixture(autouse=True)
def public_test_boundaries(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
    ])
    monkeypatch.setattr(main, "_has_valid_cookies", lambda: False)
    monkeypatch.setattr(main, "PROXY_URL", None)
    monkeypatch.setattr(main, "_extract_cache", main.TTLCache(16, 60))
    monkeypatch.setattr(main.RateLimiter, "is_allowed", lambda *args: True)


@pytest.fixture
def instagram_product(monkeypatch):
    def install(media):
        product = {
            "pk": "314159", "user": {"username": "fixture", "full_name": "Fixture"},
            "caption": {"text": "A fixture Instagram post"}, **deepcopy(media),
        }

        def extract(self, url):
            assert url == POST_URL
            return self._extract_product(deepcopy(product), "DefCkhRikbG", get_comments=False)

        monkeypatch.setattr(InstagramIE, "_real_extract", extract)
        monkeypatch.setattr(InstagramIE, "_real_initialize", lambda self: None)

        def reject_fallback(url):
            raise HTTPException(status_code=400, detail="Photo extraction unexpectedly required the API fallback")

        monkeypatch.setattr(main, "extract_instagram", reject_fallback)

    return install


@pytest.mark.parametrize(("product", "expected_urls"), [
    ({"carousel_media": [_photo(index) for index in range(6)]}, PHOTO_URLS),
    (_photo(0), [PHOTO_URLS[0]]),
    (_photo_without_dimensions(0), [PHOTO_URLS[0]]),
    ({"carousel_media": [_photo(0), _video(), _photo(1)]}, [PHOTO_URLS[0], VIDEO_URL, PHOTO_URLS[1]]),
], ids=["six-photo-carousel", "single-photo", "photo-without-dimensions", "mixed-photo-video-carousel"])
def test_extract_instagram_preserves_all_media_in_order(product, expected_urls, instagram_product):
    instagram_product(product)
    with TestClient(main.app, base_url="http://localhost") as client:
        response = client.post("/api/v1/extract", json={"url": POST_URL})
    assert response.status_code == 200, response.text
    assert response.json()["media_urls"] == expected_urls
    assert response.json()["caption"] == "A fixture Instagram post"


@pytest.fixture
def selected_media_transport(monkeypatch, tmp_path):
    source_downloads = []
    requests = []
    open_resources = []
    bodies = {PHOTO_URLS[3]: b"\xff\xd8\xfffixture-selected-photo\xff\xd9", VIDEO_URL: b"fixture-selected-video"}

    def reject_reextraction(*args, **kwargs):
        source_downloads.append(args)
        raise AssertionError("Selected Instagram media must not reextract the original carousel")

    class MediaResponse:
        status_code = 200

        def __init__(self, url):
            assert url in bodies
            self.content = bodies[url]
            self.headers = {"Content-Type": "image/jpeg" if url == PHOTO_URLS[3] else "video/mp4",
                            "Content-Length": str(len(self.content))}

        async def aiter_content(self, *args, **kwargs):
            yield self.content[:5]
            yield self.content[5:]

    class MediaSession:
        async def __aenter__(self):
            open_resources.append(self)
            return self

        async def __aexit__(self, *args):
            open_resources.remove(self)

        async def head(self, url, **kwargs):
            requests.append(("HEAD", url))
            response = MediaResponse(url)
            response.status_code = 405  # Media CDNs may allow GET while rejecting HEAD.
            return response

        @asynccontextmanager
        async def stream(self, method, url, **kwargs):
            requests.append((method, url))
            open_resources.append(url)
            try:
                yield MediaResponse(url)
            finally:
                open_resources.remove(url)

        async def get(self, url, **kwargs):
            requests.append(("GET", url))
            return MediaResponse(url)

    monkeypatch.setattr(main, "SafeYoutubeDL", reject_reextraction)
    monkeypatch.setattr(main, "_new_async_session", MediaSession)
    monkeypatch.setattr(main, "DOWNLOAD_DIR", tmp_path)
    return source_downloads, requests, bodies, open_resources


@pytest.mark.parametrize(("url", "extension", "media_type"), [
    (PHOTO_URLS[3], "jpg", "image/jpeg"), (VIDEO_URL, "mp4", "video/mp4"),
], ids=["selected-photo", "selected-video"])
def test_download_instagram_delivers_selected_media_without_reextracting(
    url, extension, media_type, selected_media_transport,
):
    source_downloads, requests, bodies, open_resources = selected_media_transport
    with TestClient(main.app, base_url="http://localhost") as client:
        response = client.post("/api/v1/download", json={"url": url, "original_url": POST_URL})
    assert response.status_code == 200, response.text
    assert source_downloads == [], "Download must fetch the selected carousel item directly"
    assert response.content == bodies[url]
    assert response.headers["content-type"] == media_type
    assert response.headers["content-disposition"] == f"attachment; filename=media.{extension}"
    assert requests == [("GET", url)]
    assert open_resources == []


@pytest.mark.parametrize(("url", "extension"), [
    (PHOTO_URLS[3], "jpg"), (VIDEO_URL, "mp4"),
], ids=["selected-photo", "selected-video"])
def test_save_instagram_writes_selected_media_without_reextracting(
    url, extension, selected_media_transport, tmp_path,
):
    source_downloads, requests, bodies, open_resources = selected_media_transport
    filename = f"selected.{extension}"
    with TestClient(main.app, base_url="http://localhost") as client:
        response = client.post("/api/v1/save", json={
            "url": url, "original_url": POST_URL, "filename": filename,
        })
    assert response.status_code == 200, response.text
    assert source_downloads == [], "Save must fetch the selected carousel item directly"
    assert (tmp_path / filename).read_bytes() == bodies[url]
    assert response.json()["size"] == len(bodies[url])
    assert requests == [("GET", url)]
    assert open_resources == []


@pytest.mark.parametrize("disconnect_at", ["http.response.start", "http.response.body"])
def test_download_closes_upstream_when_client_disconnects_during_send(
    disconnect_at, selected_media_transport,
):
    _, requests, _, open_resources = selected_media_transport

    async def exercise_response():
        response = await main._proxy_stream_download(main.DownloadRequest(
            url=PHOTO_URLS[3], original_url=POST_URL,
        ))
        assert len(open_resources) == 2

        async def receive():
            raise AssertionError("ASGI 2.4 reports disconnects through send")

        async def send(message):
            if message["type"] == disconnect_at:
                raise OSError("Client disconnected")

        try:
            with pytest.raises(ClientDisconnect):
                await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
            assert open_resources == [], "Upstream session and response leaked after the client disconnected"
        finally:
            await response.body_iterator.aclose()

    asyncio.run(exercise_response())
    assert requests == [("GET", PHOTO_URLS[3])]
