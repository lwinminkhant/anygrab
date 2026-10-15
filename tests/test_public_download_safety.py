"""Public download boundaries reject internal requests and unsafe writes."""
import socket
from pathlib import Path
from unittest.mock import patch, Mock

import pytest

from public_media_safety import validate_url, validate_headers, safe_download_path, SafeYoutubeDL, PublicRequestGuard


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))])


@pytest.mark.parametrize('url', [
    'http://127.0.0.1/', 'http://169.254.169.254/', 'file:///etc/passwd',
    'https://youtube.com.evil.example/', 'https://evil.example/?youtube.com',
    'https://user:pass@youtube.com/watch?v=1', 'https://youtube.com:8080/watch?v=1',
    'https://youtube.com/\nwatch', 'https://youtube.com\\@evil.example/',
])
def test_rejects_unsafe_urls(url):
    with pytest.raises(ValueError):
        validate_url(url)


def test_source_rejects_cdn_but_media_accepts_it():
    url = 'https://rr1.googlevideo.com/videoplayback?x=1'
    assert validate_url(url) == url
    with pytest.raises(ValueError):
        validate_url(url, source=True)


@pytest.mark.parametrize('address', ['127.0.0.1', '10.0.0.1', '169.254.169.254', '::1', '::ffff:127.0.0.1'])
def test_rejects_nonpublic_dns(monkeypatch, address):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args: [(socket.AF_INET, 1, 6, '', (address, 443))])
    with pytest.raises(ValueError):
        validate_url('https://youtube.com/watch?v=1')


@pytest.mark.parametrize('filename', ['../main.py', '/app/main.py', '..\\main.py', '.', '..', 'evil\x00.mp4'])
def test_rejects_path_escape(tmp_path, filename):
    with pytest.raises(ValueError):
        safe_download_path(tmp_path, filename)


def test_accepts_simple_filename(tmp_path):
    assert safe_download_path(tmp_path, 'my clip.mp4') == tmp_path / 'my clip.mp4'


def test_rejects_symlink_escape(tmp_path):
    (tmp_path / 'clip.mp4').symlink_to('/tmp/outside-media')
    with pytest.raises(ValueError):
        safe_download_path(tmp_path, 'clip.mp4')


@pytest.mark.parametrize('headers', [{'Host': 'internal'}, {'Authorization': 'secret'}, {'Cookie': 'token'}, {'User-Agent': 'x\r\nHost: internal'}])
def test_rejects_unsafe_headers(headers):
    with pytest.raises(ValueError):
        validate_headers(headers)


def test_preserves_media_headers():
    headers = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.youtube.com/', 'Accept': '*/*'}
    assert validate_headers(headers) == headers


def test_redirect_request_guard_checks_each_destination():
    from urllib.request import Request
    guard = PublicRequestGuard()
    assert guard.https_request(Request('https://youtube.com/watch?v=1'))
    with pytest.raises(ValueError):
        guard.http_request(Request('http://127.0.0.1/admin'))


def test_downloader_disables_generic_and_impersonation():
    with SafeYoutubeDL({'quiet': True, 'impersonate': 'chrome'}) as downloader:
        assert downloader.params['allowed_extractors'] == ['(?!Generic).*']
        assert not downloader.params.get('impersonate')
        director = downloader.build_request_director([])
        assert len(director.handlers) == 1


def test_actual_urllib_opener_blocks_unsafe_redirect():
    import io
    from email.message import Message
    from urllib.request import HTTPHandler
    from urllib.response import addinfourl
    from public_media_safety import PublicUrllibRH

    calls = []

    class FakeNetwork(HTTPHandler):
        handler_order = 1

        def http_open(self, request):
            calls.append(request.full_url)
            headers = Message()
            headers['Location'] = 'http://127.0.0.1/private'
            response = addinfourl(io.BytesIO(b''), headers, request.full_url, 302)
            response.msg = 'Found'
            return response

    handler = PublicUrllibRH(logger=Mock())
    opener = handler._create_instance(proxies={}, cookiejar=None)
    opener.add_handler(FakeNetwork())
    with pytest.raises(ValueError, match='supported public media hosts'):
        opener.open('http://youtube.com/watch?v=1')
    assert calls == ['http://youtube.com/watch?v=1']


def test_curl_transport_forces_redirects_off():
    from curl_cffi.requests import Session
    from public_media_safety import PublicSession
    with patch.object(Session, 'request', return_value='ok') as request:
        with PublicSession() as session:
            assert session.get('https://youtube.com/watch?v=1', allow_redirects=True) == 'ok'
        assert request.call_args.kwargs['allow_redirects'] is False


def test_public_api_rejects_attacks_before_network(monkeypatch):
    from fastapi.testclient import TestClient
    import main
    monkeypatch.setattr(main.RateLimiter, 'is_allowed', lambda self, address: True)
    with TestClient(main.app, base_url="http://localhost") as client:
        assert client.post('/api/v1/extract', json={'url': 'https://evil.example/?youtube.com'}).status_code == 422
        assert client.post('/api/v1/download', json={'url': 'http://127.0.0.1/'}).status_code == 422
        assert client.get('/api/v1/download', params={'url': 'http://127.0.0.1/'}).status_code == 400
        assert client.post('/api/v1/save', json={'url': 'https://rr1.googlevideo.com/a', 'filename': '/app/main.py'}).status_code == 422
