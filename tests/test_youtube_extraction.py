"""YouTube requests must resolve one video without traversing collections."""
import asyncio
import socket
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import HTTPException
from fastapi.responses import Response
from fastapi.testclient import TestClient
from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.extractor.youtube import YoutubePlaylistIE
from yt_dlp.utils import DownloadError

import main
from public_media_safety import SafeYoutubeDL


VIDEO_ID = "esrPUSvaDNE"
MEDIA_URL = "https://rr1.googlevideo.com/fixture.mp4"
COLLECTION_URLS = (
    "https://www.youtube.com/@fixture/videos",
    "https://www.youtube.com/channel/UCfixture",
    "https://www.youtube.com/playlist?list=PLfixture",
    "https://www.youtube.com/embed/videoseries?list=PLfixture",
)


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
    ])
    monkeypatch.setattr(main, "_has_valid_cookies", lambda: False)
    monkeypatch.setattr(main, "PROXY_URL", None)
    monkeypatch.setattr(main.RateLimiter, "is_allowed", lambda *args: True)


@pytest.fixture
def fixture_extractor(monkeypatch):
    """Exercise real yt-dlp playlist processing, with no HTTP extraction."""
    extracted_videos = []
    yielded_entries = []

    class FixtureYoutubeIE(InfoExtractor):
        _VALID_URL = r"https?://(?:www\.)?(?:youtube\.com|youtu\.be|instagram\.com)/.+"

        def _real_extract(self, url):
            parsed = urlsplit(url)
            query = parse_qs(parsed.query)
            video_id = query.get("v", [None])[0]
            if parsed.hostname == "youtu.be" or parsed.path.startswith((
                "/shorts/", "/live/", "/embed/", "/v/", "/clip/",
            )):
                video_id = parsed.path.rsplit("/", 1)[-1]
            redirect_type = query.get("fixture_redirect", [None])[0]
            if redirect_type:
                result = self.url_result(
                    f"https://www.youtube.com/watch?v={VIDEO_ID}&fixture_type=playlist",
                    ie=FixtureYoutubeIE.ie_key(), video_id=VIDEO_ID,
                )
                return {**result, "_type": redirect_type}
            playlist_id = query.get("list", [None])[0]
            unexpected_collection = query.get("fixture_type", [None])[0]
            if unexpected_collection or not video_id or self._yes_playlist(playlist_id, video_id):
                def entries():
                    for entry_id in (VIDEO_ID, "dFkQydVNXRQ"):
                        yielded_entries.append(entry_id)
                        yield self.url_result(
                            f"https://www.youtube.com/watch?v={entry_id}",
                            ie=FixtureYoutubeIE.ie_key(), video_id=entry_id,
                        )

                result = self.playlist_result(entries(), "fixture-collection")
                return {**result, "_type": unexpected_collection or "playlist"}
            extracted_videos.append(video_id)
            return {
                "id": video_id, "title": "Fixture video", "url": MEDIA_URL,
                "ext": "mp4", "vcodec": "h264", "acodec": "aac",
            }

    class FixtureYoutubeDL(SafeYoutubeDL):
        def add_default_info_extractors(self):
            self.add_info_extractor(FixtureYoutubeIE())

        def urlopen(self, request):
            raise AssertionError("Regression tests must never access the network")

    monkeypatch.setattr(main, "SafeYoutubeDL", FixtureYoutubeDL)
    return extracted_videos, yielded_entries


@pytest.mark.parametrize("url", COLLECTION_URLS)
def test_collection_extraction_does_not_visit_video_entries(url, fixture_extractor):
    with pytest.raises((HTTPException, ValueError)):
        main.extract_with_ytdlp(url, "youtube")
    extracted_videos, yielded_entries = fixture_extractor
    assert extracted_videos == []
    assert yielded_entries == []


@pytest.mark.parametrize("url", [
    f"https://www.youtube.com/watch?v={VIDEO_ID}",
    f"https://www.youtube.com/watch?v={VIDEO_ID}&list=PLfixture",
    f"https://youtu.be/{VIDEO_ID}?list=PLfixture",
    f"https://www.youtube.com/shorts/{VIDEO_ID}",
    f"https://www.youtube.com/live/{VIDEO_ID}",
    f"https://www.youtube.com/embed/{VIDEO_ID}",
    f"https://www.youtube.com/v/{VIDEO_ID}",
    f"https://www.youtube.com/clip/{VIDEO_ID}",
])
def test_single_video_extraction_never_expands_playlist(url, fixture_extractor):
    response = main.extract_with_ytdlp(url, "youtube")
    extracted_videos, yielded_entries = fixture_extractor
    assert response.media_urls == [MEDIA_URL]
    assert response.metadata["id"] == VIDEO_ID
    assert extracted_videos == [VIDEO_ID]
    assert yielded_entries == []


@pytest.mark.parametrize("collection_type", ["playlist", "multi_video"])
def test_unexpected_collection_is_rejected_before_iteration(collection_type, fixture_extractor):
    url = f"https://www.youtube.com/watch?v={VIDEO_ID}&fixture_type={collection_type}"
    with pytest.raises(HTTPException) as error:
        main.extract_with_ytdlp(url, "youtube")
    assert error.value.status_code == 400
    extracted_videos, yielded_entries = fixture_extractor
    assert extracted_videos == []
    assert yielded_entries == []


@pytest.mark.parametrize("redirect_type", ["url", "url_transparent"])
@pytest.mark.parametrize("path", [f"watch?v={VIDEO_ID}&", f"clip/{VIDEO_ID}?"])
def test_nested_collection_is_rejected_before_iteration(path, redirect_type, fixture_extractor):
    url = f"https://www.youtube.com/{path}fixture_redirect={redirect_type}"
    with pytest.raises(HTTPException) as error:
        main.extract_with_ytdlp(url, "youtube")
    assert error.value.status_code == 400
    extracted_videos, yielded_entries = fixture_extractor
    assert extracted_videos == []
    assert yielded_entries == []


@pytest.mark.parametrize("redirect_type", ["url", "url_transparent"])
@pytest.mark.parametrize("operation", ["stream", "save"])
def test_download_helpers_reject_nested_collections(
    redirect_type, operation, fixture_extractor, monkeypatch, tmp_path,
):
    monkeypatch.setattr(main, "DOWNLOAD_DIR", tmp_path)
    monkeypatch.setattr(main.tempfile, "gettempdir", lambda: str(tmp_path))
    request = main.SaveRequest(
        url=MEDIA_URL,
        original_url=f"https://www.youtube.com/clip/{VIDEO_ID}?fixture_redirect={redirect_type}",
        filename="fixture.mp4",
    )
    if operation == "stream":
        result = asyncio.run(main._ytdlp_stream_download(request, "youtube"))
    else:
        result = asyncio.run(main._save_via_ytdlp(request, tmp_path / "fixture.mp4", "youtube"))
    extracted_videos, yielded_entries = fixture_extractor
    assert result is None
    assert extracted_videos == []
    assert yielded_entries == []
    assert list(tmp_path.iterdir()) == []


def test_actual_embedded_playlist_redirect_cannot_traverse_collection():
    tab_urls = []
    yielded_entries = []

    class FixtureTabIE(InfoExtractor):
        _VALID_URL = r"https://www\.youtube\.com/playlist\?.+"

        @classmethod
        def ie_key(cls):
            return "YoutubeTab"

        def _real_extract(self, url):
            tab_urls.append(url)

            def entries():
                yielded_entries.append(VIDEO_ID)
                yield {"id": VIDEO_ID, "title": "Fixture", "url": MEDIA_URL}

            return self.playlist_result(entries(), "PL0123456789abcdef")

    class EmbeddedPlaylistDL(SafeYoutubeDL):
        def add_default_info_extractors(self):
            self.add_info_extractor(YoutubePlaylistIE())
            self.add_info_extractor(FixtureTabIE())

        def urlopen(self, request):
            raise AssertionError("Regression tests must never access the network")

    with EmbeddedPlaylistDL({"quiet": True}, single_video=True) as downloader:
        with pytest.raises(DownloadError, match="single YouTube video"):
            downloader.extract_info(
                "https://www.youtube.com/embed/videoseries?list=PL0123456789abcdef", download=False,
            )
    assert tab_urls == ["https://www.youtube.com/playlist?list=PL0123456789abcdef"]
    assert yielded_entries == []


def test_other_platform_carousels_still_include_every_item(fixture_extractor):
    response = main.extract_with_ytdlp("https://www.instagram.com/p/fixture/", "instagram")
    extracted_videos, yielded_entries = fixture_extractor
    assert response.media_urls == [MEDIA_URL, MEDIA_URL]
    assert extracted_videos == [VIDEO_ID, "dFkQydVNXRQ"]
    assert yielded_entries == [VIDEO_ID, "dFkQydVNXRQ"]


@pytest.mark.parametrize("url", COLLECTION_URLS)
@pytest.mark.parametrize("endpoint", ["extract", "download", "download_get", "save"])
def test_api_rejects_collection_sources_before_work(url, endpoint, monkeypatch):
    extract = Mock(return_value=main.MediaResponse(
        platform="youtube", caption="Unexpected extraction", media_urls=[MEDIA_URL], metadata={},
    ))
    download = AsyncMock(return_value=Response(content=b"unexpected"))
    save = AsyncMock(return_value=main.SaveResponse(
        success=True, filename="fixture.mp4", path="fixture.mp4", size=0,
    ))
    monkeypatch.setattr(main, "extract_with_ytdlp", extract)
    monkeypatch.setattr(main, "_ytdlp_stream_download", download)
    monkeypatch.setattr(main, "_save_via_ytdlp", save)
    payload = {"url": MEDIA_URL, "original_url": url}
    with TestClient(main.app, base_url="http://localhost") as client:
        if endpoint == "extract":
            response = client.post("/api/v1/extract", json={"url": url})
        elif endpoint == "download_get":
            response = client.get("/api/v1/download", params=payload)
        else:
            response = client.post(f"/api/v1/{endpoint}", json=payload)
    assert response.status_code in (400, 422), response.text
    extract.assert_not_called()
    download.assert_not_awaited()
    save.assert_not_awaited()
