"""Boundaries for the anonymous public downloader and its outbound redirects."""
import ipaddress
import io
from pathlib import Path
import socket
from urllib.parse import parse_qs, urlsplit
from urllib.request import BaseHandler

import yt_dlp
from yt_dlp.networking._urllib import UrllibRH
from yt_dlp.utils import expand_path, is_path_like
from instagram_media import InstagramMediaIE

SOURCE_HOSTS = ('youtube.com', 'youtu.be', 'tiktok.com', 'instagram.com', 'facebook.com', 'fb.watch', 'twitter.com', 'x.com')
MEDIA_HOSTS = SOURCE_HOSTS + (
    'googlevideo.com', 'ytimg.com', 'youtubei.googleapis.com', 'googleapis.com',
    'cdninstagram.com', 'fbcdn.net', 'tiktokcdn.com', 'tiktokcdn-us.com',
    'tiktokcdn-eu.com', 'tiktokv.com', 'tiktokv.us', 'ibytedtos.com',
    'ibyteimg.com', 'byteoversea.com', 'byteoversea.net', 'muscdn.com',
    'musical.ly', 'tikwm.com', 'akamaized.net', 'twimg.com',
)
SAFE_HEADERS = frozenset(('user-agent', 'referer', 'accept', 'accept-language', 'accept-encoding', 'range', 'origin', 'sec-fetch-mode'))


def _validate_youtube_source(parsed):
    host = (parsed.hostname or '').lower()
    parts = parsed.path.strip('/').split('/')
    if host == 'youtu.be' or host.endswith('.youtu.be'):
        single_video = len(parts) == 1 and bool(parts[0])
    elif host == 'youtube.com' or host.endswith('.youtube.com'):
        single_video = (
            parts == ['watch'] and bool(parse_qs(parsed.query).get('v', [''])[0])
            or (len(parts) == 2 and parts[0] in ('shorts', 'embed', 'live', 'v', 'clip')
                and bool(parts[1]) and parts[1] != 'videoseries')
        )
    else:
        return
    if not single_video:
        raise ValueError('Use a single YouTube video link, not a channel or playlist.')


def validate_url(value, *, source=False):
    if not isinstance(value, str) or not value or len(value) > 8192 or '\\' in value or any(ord(char) < 33 for char in value):
        raise ValueError('Enter a supported public media URL.')
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or '').lower()
        port = parsed.port
    except ValueError:
        raise ValueError('Enter a supported public media URL.') from None
    allowed = SOURCE_HOSTS if source else MEDIA_HOSTS
    if (parsed.scheme not in ('https', 'http') or parsed.username is not None or parsed.password is not None
            or port not in (None, 80 if parsed.scheme == 'http' else 443)
            or not any(host == domain or host.endswith('.' + domain) for domain in allowed)):
        raise ValueError('Only supported public media hosts are allowed.')
    if source:
        _validate_youtube_source(parsed)
    try:
        addresses = socket.getaddrinfo(host, port or (443 if parsed.scheme == 'https' else 80))
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise ValueError('Media URLs must resolve to public addresses.')
    except (OSError, ValueError):
        raise ValueError('Media URLs must resolve to public addresses.') from None
    return value


def validate_headers(headers):
    if len(headers) > 16:
        raise ValueError('Too many media request headers.')
    for name, value in headers.items():
        if name.lower() not in SAFE_HEADERS or len(value) > 8192 or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError('Unsupported media request header.')
        if name.lower() in ('referer', 'origin'):
            validate_url(value)
    return dict(headers)


def safe_download_path(directory, filename):
    if not filename or filename in ('.', '..') or len(filename) > 200 or any(char in filename for char in ('/', '\\')) or any(ord(char) < 32 for char in filename):
        raise ValueError('Use a filename without directory paths.')
    root = Path(directory).resolve()
    result = root / filename
    if result.is_symlink() or result.resolve().parent != root:
        raise ValueError('The download must stay inside the download directory.')
    return result


class PublicRequestGuard(BaseHandler):
    # urllib executes request processors again when following a redirect.
    handler_order = 100

    def http_request(self, request):
        validate_url(request.full_url)
        return request

    https_request = http_request


class PublicUrllibRH(UrllibRH):
    def _create_instance(self, *args, **kwargs):
        opener = super()._create_instance(*args, **kwargs)
        opener.add_handler(PublicRequestGuard())
        return opener


class _RuntimeCookieFile(io.StringIO):
    def truncate(self, size=None):
        # yt-dlp truncates file-like cookie stores before saving, but unlike a
        # newly opened disk file their cursor still follows the previous read.
        if size == 0:
            self.seek(0)
        return super().truncate(size)


class SafeYoutubeDL(yt_dlp.YoutubeDL):
    def __init__(self, params=None, *args, single_video=False, **kwargs):
        self._single_video = single_video
        options = {**(params or {}), 'allowed_extractors': ['(?!Generic).*'], 'enable_file_urls': False}
        options.pop('impersonate', None)
        cookiefile = options.get('cookiefile')
        if is_path_like(cookiefile):
            # Deployment secrets can be read-only. Keep yt-dlp's updated
            # cookies in a private memory store instead of rewriting the source.
            with open(expand_path(cookiefile), encoding='utf-8') as source:
                options['cookiefile'] = _RuntimeCookieFile(source.read())
        super().__init__(options, *args, **kwargs)
        self.add_info_extractor(InstagramMediaIE())

    def process_ie_result(self, ie_result, download=True, extra_info=None):
        # This also runs for URL results resolved recursively by yt-dlp.
        if self._single_video and ie_result.get('_type') in ('playlist', 'multi_video'):
            raise yt_dlp.utils.DownloadError('Use a single YouTube video link, not a channel or playlist.')
        return super().process_ie_result(ie_result, download, extra_info)

    def build_request_director(self, handlers, preferences=None):
        # A single guarded HTTP transport covers API calls, media and redirects.
        return super().build_request_director([PublicUrllibRH], preferences=None)


# curl does not expose a per-redirect destination validator. Block redirects
# entirely; yt-dlp's guarded transport handles source short links instead.
from curl_cffi.requests import Session, AsyncSession


class PublicSession(Session):
    def request(self, method, url, *args, **kwargs):
        validate_url(url)
        return super().request(method, url, *args, **{**kwargs, 'allow_redirects': False})


class PublicAsyncSession(AsyncSession):
    async def request(self, method, url, *args, **kwargs):
        validate_url(url)
        return await super().request(method, url, *args, **{**kwargs, 'allow_redirects': False})
