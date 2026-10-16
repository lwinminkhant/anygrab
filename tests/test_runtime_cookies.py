"""Read-only deployment cookies stay private to each downloader instance."""
import builtins
import errno
from http.cookiejar import Cookie
from pathlib import Path

import pytest
from yt_dlp.utils import DownloadError

from public_media_safety import SafeYoutubeDL


COOKIES = (
    '# Netscape HTTP Cookie File\n'
    '#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t2147483647\tSID\tfixture-session\n'
)


@pytest.fixture
def cookie_secret(tmp_path, monkeypatch):
    source = tmp_path / 'cookies.txt'
    source.write_text(COOKIES, encoding='utf-8')
    original_open = builtins.open

    def read_only_open(file, mode='r', *args, **kwargs):
        if isinstance(file, (str, bytes, Path)) and Path(file) == source and any(char in mode for char in 'wax+'):
            raise OSError(errno.EROFS, 'Read-only file system')
        return original_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, 'open', read_only_open)
    return source


def _downloader(cookiefile):
    return SafeYoutubeDL({'cookiefile': cookiefile, 'quiet': True}, auto_init=False)


@pytest.mark.parametrize('as_string', [False, True])
def test_read_only_cookie_secret_loads_and_saves_on_exit(cookie_secret, as_string):
    source = str(cookie_secret) if as_string else cookie_secret
    options = {'cookiefile': source, 'quiet': True}
    with SafeYoutubeDL(options, auto_init=False) as downloader:
        cookies = list(downloader.cookiejar)
        assert len(cookies) == 1
        assert cookies[0].name == 'SID'
        assert cookies[0].value == 'fixture-session'
        assert cookies[0].domain == '.youtube.com'
        assert cookies[0].secure is True
        downloader.save_cookies()
        downloader.save_cookies()
        # Repeated saves must produce valid Netscape text without null padding.
        downloader.cookiejar.clear()
        downloader.cookiejar.filename.seek(0)
        downloader.cookiejar.load()
        assert len(downloader.cookiejar) == 1
        assert '\x00' not in downloader.cookiejar.filename.getvalue()
    assert options['cookiefile'] == source
    assert cookie_secret.read_text(encoding='utf-8') == COOKIES


def test_cookie_updates_are_isolated_between_downloaders(cookie_secret):
    with _downloader(cookie_secret) as first, _downloader(cookie_secret) as second:
        cookie = next(iter(first.cookiejar))
        first.cookiejar.set_cookie(Cookie(
            version=0, name='runtime', value='new-value', port=None,
            port_specified=False, domain=cookie.domain, domain_specified=True,
            domain_initial_dot=True, path='/', path_specified=True,
            secure=True, expires=None, discard=True, comment=None,
            comment_url=None, rest={}, rfc2109=False,
        ))
        first.save_cookies()
        assert {item.name for item in first.cookiejar} == {'SID', 'runtime'}
        assert {item.name for item in second.cookiejar} == {'SID'}
        assert first.cookiejar.filename is not second.cookiejar.filename
    assert cookie_secret.read_text(encoding='utf-8') == COOKIES


def test_missing_cookie_file_fails_clearly(tmp_path):
    with pytest.raises(FileNotFoundError):
        _downloader(tmp_path / 'missing.txt')


def test_malformed_cookie_file_fails_clearly(tmp_path):
    source = tmp_path / 'malformed.txt'
    source.write_text('{"cookies": []}', encoding='utf-8')
    with pytest.raises(DownloadError, match='Netscape|failed to load cookies'):
        with _downloader(source) as downloader:
            _ = downloader.cookiejar
