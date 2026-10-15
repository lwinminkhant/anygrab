"""TikTok fallback extraction and selected-media delivery regressions."""
from contextlib import asynccontextmanager
import socket
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import main


POST_URL = ("https://www.tiktok.com/@beausbrain/video/7696561269729545486"
            "?is_from_webapp=1&sender_device=pc")
MEDIA_URL = "https://v16.tiktokcdn.com/fixture-selected.mp4"
PROVIDER_MEDIA_URL = "https://www.tikwm.com/video/media/hdplay/fixture.mp4"
PHOTO_URL = "https://p16.tiktokcdn.com/fixture-selected.jpg"


@pytest.fixture(autouse=True)
def public_test_boundaries(monkeypatch, tmp_path):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
    ])
    monkeypatch.setattr(main, "_has_valid_cookies", lambda: False)
    monkeypatch.setattr(main, "PROXY_URL", None)
    monkeypatch.setattr(main, "DOWNLOAD_DIR", tmp_path)
    monkeypatch.setattr(main, "_extract_cache", main.TTLCache(16, 60))
    monkeypatch.setattr(main.RateLimiter, "is_allowed", lambda *args: True)


@pytest.fixture
def tiktok_transport(monkeypatch):
    calls = []
    open_resources = []
    state = {"status": 200, "content_type": "video/mp4", "body": b"selected-tiktok-media",
             "provider_code": 0}

    class ProviderResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"code": state["provider_code"], "msg": "provider fixture", "data": {
                "id": "7696561269729545486", "title": "Fixture TikTok", "duration": 3,
                "hdplay": PROVIDER_MEDIA_URL,
            }}

    class ProviderSession:
        def __enter__(self):
            open_resources.append(self)
            return self

        def __exit__(self, *args):
            open_resources.remove(self)

        def get(self, url, **kwargs):
            prepared = httpx.Request("GET", url, params=kwargs.get("params"))
            calls.append(("provider", str(prepared.url)))
            return ProviderResponse()

    class MediaResponse:
        def __init__(self):
            self.status_code = state["status"]
            self.content = state["body"]
            self.headers = {"Content-Type": state["content_type"], "Content-Length": str(len(self.content))}

        async def aiter_content(self, *args, **kwargs):
            yield self.content[:5]
            yield self.content[5:]

    class MediaSession:
        async def __aenter__(self):
            open_resources.append(self)
            return self

        async def __aexit__(self, *args):
            open_resources.remove(self)

        @asynccontextmanager
        async def stream(self, method, url, **kwargs):
            calls.append((method, url))
            open_resources.append(url)
            try:
                yield MediaResponse()
            finally:
                open_resources.remove(url)

        async def get(self, url, **kwargs):
            calls.append(("GET", url))
            return MediaResponse()

    monkeypatch.setattr(main, "_new_session", ProviderSession)
    monkeypatch.setattr(main, "_new_async_session", MediaSession)
    return state, calls, open_resources


def test_provider_receives_the_complete_source_url_as_one_parameter(tiktok_transport):
    _, calls, open_resources = tiktok_transport
    result = main.extract_tiktok_fallback(POST_URL)
    assert result.media_urls == [PROVIDER_MEDIA_URL]
    assert len(calls) == 1
    provider_query = parse_qs(urlsplit(calls[0][1]).query)
    assert provider_query == {"url": [POST_URL], "hd": ["1"]}
    assert open_resources == []


@pytest.mark.parametrize("provider_fails", [False, True], ids=["provider-preferred", "direct-backup-once"])
def test_extract_prefers_provider_and_uses_direct_backup_only_after_failure(
    provider_fails, tiktok_transport, monkeypatch,
):
    state, calls, open_resources = tiktok_transport
    state["provider_code"] = -1 if provider_fails else 0
    direct = Mock(return_value=main.MediaResponse(
        platform="tiktok", media_urls=[MEDIA_URL], metadata={"id": "direct-backup"},
    ))
    monkeypatch.setattr(main, "extract_with_ytdlp", direct)
    with TestClient(main.app, base_url="http://localhost") as client:
        response = client.post("/api/v1/extract", json={"url": POST_URL})
    assert response.status_code == 200, response.text
    assert len([call for call in calls if call[0] == "provider"]) == 1
    assert response.json()["media_urls"] == ([MEDIA_URL] if provider_fails else [PROVIDER_MEDIA_URL])
    if provider_fails:
        direct.assert_called_once_with(POST_URL, "tiktok", allow_tiktok_fallback=False)
    else:
        direct.assert_not_called()
    assert open_resources == []


def test_direct_backup_does_not_retry_the_failed_provider(monkeypatch):
    direct = Mock(side_effect=RuntimeError("TikTok direct extraction failed"))
    provider = Mock(side_effect=AssertionError("The provider already failed and must not be retried"))
    monkeypatch.setattr(main, "_run_ytdlp_extract_with_fallback", direct)
    monkeypatch.setattr(main, "extract_tiktok_fallback", provider)
    with pytest.raises(HTTPException) as error:
        main.extract_with_ytdlp(POST_URL, "tiktok", allow_tiktok_fallback=False)
    assert error.value.status_code == 400
    assert "TikTok direct extraction failed" in error.value.detail
    direct.assert_called_once()
    provider.assert_not_called()


@pytest.mark.parametrize("endpoint", ["download", "save"])
@pytest.mark.parametrize("media_url", [MEDIA_URL, PHOTO_URL], ids=["video", "photo"])
def test_selected_tiktok_media_is_delivered_without_refetching_the_post(
    endpoint, media_url, tiktok_transport, monkeypatch, tmp_path,
):
    state, calls, open_resources = tiktok_transport
    photo = media_url == PHOTO_URL
    state["content_type"] = "image/jpeg" if photo else "video/mp4"
    direct = Mock(side_effect=AssertionError("The selected media URL must not reextract the post"))
    monkeypatch.setattr(main, "SafeYoutubeDL", direct)
    filename = "selected.jpg" if photo else "selected.mp4"
    with TestClient(main.app, base_url="http://localhost") as client:
        response = client.post(f"/api/v1/{endpoint}", json={
            "url": media_url, "original_url": POST_URL, "filename": filename,
        })
    assert response.status_code == 200, response.text
    assert calls == [("GET", media_url)], "Selected downloads must not refetch the provider or choose another item"
    direct.assert_not_called()
    if endpoint == "download":
        assert response.content == state["body"]
        assert response.headers["content-type"] == state["content_type"]
        extension = "jpg" if photo else "mp4"
        assert response.headers["content-disposition"] == f"attachment; filename=media.{extension}"
    else:
        assert (tmp_path / filename).read_bytes() == state["body"]
        assert response.json()["size"] == len(state["body"])
    assert open_resources == []


@pytest.mark.parametrize("endpoint", ["download", "save"])
def test_tiktok_upstream_error_is_not_delivered_as_a_successful_video(
    endpoint, tiktok_transport, monkeypatch, tmp_path,
):
    state, calls, open_resources = tiktok_transport
    state.update({"status": 403, "body": b"Forbidden", "content_type": "text/html"})
    monkeypatch.setattr(main, "_ytdlp_stream_download", AsyncMock(return_value=None))
    monkeypatch.setattr(main, "_save_via_ytdlp", AsyncMock(return_value=None))
    with TestClient(main.app, base_url="http://localhost") as client:
        response = client.post(f"/api/v1/{endpoint}", json={
            "url": MEDIA_URL, "original_url": POST_URL, "filename": "forbidden.mp4",
        })
    assert response.status_code == 502, response.text
    assert not (tmp_path / "forbidden.mp4").exists()
    assert calls == [("GET", MEDIA_URL)]
    assert open_resources == []
