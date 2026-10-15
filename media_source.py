"""Bounded server downloads from supported public social source URLs."""
from itertools import islice
import ipaddress
import os
import socket
import time

import httpx
from contextlib import contextmanager
from pathlib import Path
import tempfile
from urllib.parse import parse_qs, urlsplit, urlunsplit

import yt_dlp

SUPPORTED_HOSTS = (
    'youtube.com', 'youtu.be', 'instagram.com', 'tiktok.com', 'facebook.com',
    'fb.watch', 'twitter.com', 'x.com', 'reddit.com', 'redd.it', 'pinterest.com',
    'pin.it', 'vimeo.com', 'dailymotion.com', 'dai.ly', 'threads.net', 'threads.com',
    'soundcloud.com', 'bilibili.com', 'b23.tv',
)
MAX_MEDIA_BYTES = int(os.getenv('ACCOUNT_MAX_MEDIA_BYTES', str(200 * 1024 * 1024)))
MAX_MEDIA_DURATION = int(os.getenv('ACCOUNT_MAX_MEDIA_DURATION', '7200'))
MAX_DOWNLOAD_SECONDS = min(300, max(1, int(os.getenv('ACCOUNT_DOWNLOAD_TIMEOUT', '300'))))
CDN_HOSTS = ('cdninstagram.com', 'fbcdn.net', 'tiktokcdn.com', 'tiktokcdn-us.com', 'tiktokcdn-eu.com', 'ibytedtos.com', 'byteoversea.com', 'tikwm.com', 'akamaized.net')


def validate_source_url(value):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(char) < 32 for char in value):
        raise ValueError('Enter a supported public source URL.')
    parsed = urlsplit(value)
    host = (parsed.hostname or '').lower().rstrip('.')
    if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError('Enter a supported HTTPS source URL.')
    if not any(host == allowed or host.endswith('.' + allowed) for allowed in SUPPORTED_HOSTS):
        raise ValueError('This source is not supported for account downloads.')
    if host.endswith('youtube.com') and not (parsed.path in ('/watch', '/watch/') and parse_qs(parsed.query).get('v') or parsed.path.startswith(('/shorts/', '/embed/', '/live/'))):
        raise ValueError('Use a single video source, not a channel or playlist.')
    return urlunsplit(('https', parsed.netloc, parsed.path, parsed.query, parsed.fragment))


def source_platform(value):
    return (urlsplit(value).hostname or '').removeprefix('www.')


def _check_metadata(info):
    if not info or info.get('is_live') or info.get('live_status') == 'is_live':
        raise ValueError('Live media cannot be saved or transcribed.')
    if (info.get('duration') or 0) > MAX_MEDIA_DURATION:
        raise ValueError('This media exceeds the maximum duration.')
    if (info.get('filesize') or info.get('filesize_approx') or 0) > MAX_MEDIA_BYTES:
        raise ValueError('This media exceeds the maximum file size.')


def _bounded_progress(progress):
    if max(progress.get('downloaded_bytes') or 0, progress.get('total_bytes') or 0) > MAX_MEDIA_BYTES:
        raise ValueError('This media exceeds the maximum file size.')


@contextmanager
def downloaded_source(source_url, media_index=0, audio_only=False):
    source_url = validate_source_url(source_url)
    if isinstance(media_index, bool) or not isinstance(media_index, int) or not 0 <= media_index <= 99:
        raise ValueError('Choose a valid media item.')
    started = time.monotonic()
    def progress(value):
        _bounded_progress(value)
        if time.monotonic() - started > MAX_DOWNLOAD_SECONDS:
            raise ValueError('The media download timed out.')
    with tempfile.TemporaryDirectory(prefix='anygrab-source-') as directory:
        options = {
            'quiet': True, 'no_warnings': True, 'noplaylist': True,
            'lazy_playlist': True, 'playlistend': 100, 'extract_flat': 'in_playlist',
            'socket_timeout': 30, 'retries': 1, 'extractor_retries': 1,
            'max_filesize': MAX_MEDIA_BYTES, 'progress_hooks': [progress],
            'outtmpl': str(Path(directory) / '%(id)s.%(ext)s'),
            'format': 'bestaudio/best' if audio_only else 'best[ext=mp4]/best',
            'cachedir': False, 'restrictfilenames': True,
            'allowed_extractors': ['(?!Generic).*'],
        }
        with yt_dlp.YoutubeDL(options) as downloader:
            try:
                info = downloader.extract_info(source_url, download=False)
            except Exception:
                host = (urlsplit(source_url).hostname or '').lower()
                if not any(host == known or host.endswith('.' + known) for known in ('instagram.com', 'tiktok.com')):
                    raise
                path = _download_social_fallback(source_url, media_index, audio_only, directory, started)
                yield path
                return
            host = (urlsplit(source_url).hostname or '').lower()
            carousel_hosts = ('instagram.com', 'tiktok.com', 'twitter.com', 'x.com', 'reddit.com', 'threads.net', 'threads.com')
            if (info or {}).get('_type') == 'playlist' and not any(host == known or host.endswith('.' + known) for known in carousel_hosts):
                raise ValueError('Use a single media post, not a playlist.')
            entries = list(islice((info or {}).get('entries') or [], 101))
            if len(entries) > 100:
                raise ValueError('This media post contains too many items.')
            if entries:
                if media_index >= len(entries):
                    raise ValueError('The selected media item is unavailable.')
                selected = entries[media_index]
            elif media_index:
                raise ValueError('The selected media item is unavailable.')
            else:
                selected = info
            if selected and selected.get('_type') in ('url', 'url_transparent'):
                selected = downloader.process_ie_result({**selected}, download=False)
            _check_metadata(selected)
            if time.monotonic() - started > MAX_DOWNLOAD_SECONDS:
                raise ValueError('The media download timed out.')
            downloader.process_info({**selected})
        files = [path for path in Path(directory).iterdir() if path.is_file() and path.suffix not in ('.part', '.ytdl', '.json')]
        if len(files) != 1 or not 0 < files[0].stat().st_size <= MAX_MEDIA_BYTES:
            raise ValueError('The media download could not be completed within the file limit.')
        yield files[0]


def _validate_cdn_url(value):
    parsed = urlsplit(value)
    host = (parsed.hostname or '').lower().rstrip('.')
    if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError('Invalid media location.')
    if not any(host == allowed or host.endswith('.' + allowed) for allowed in CDN_HOSTS):
        raise ValueError('Unsupported media location.')
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(address[4][0]).is_global for address in addresses):
        raise ValueError('Media location must be public.')
    return value


def _download_social_fallback(source_url, media_index, audio_only, directory, started):
    # Reuse the existing server extractors; browser media URLs and headers are never accepted.
    from main import extract_instagram, extract_tiktok_fallback
    host = (urlsplit(source_url).hostname or '').lower()
    extractor = extract_instagram if host.endswith('instagram.com') else extract_tiktok_fallback
    result = extractor(source_url)
    _check_metadata(result.metadata)
    if media_index >= len(result.media_urls):
        raise ValueError('The selected media item is unavailable.')
    url = result.media_urls[media_index]
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        for _ in range(4):
            _validate_cdn_url(url)
            with client.stream('GET', url, headers={'Referer': source_url}) as response:
                if response.is_redirect:
                    url = response.headers.get('location', '')
                    continue
                response.raise_for_status()
                mime = response.headers.get('content-type', '').split(';')[0]
                if mime.startswith('image/') and audio_only:
                    raise ValueError('This media item has no audio.')
                if not mime.startswith(('image/', 'video/', 'audio/')):
                    raise ValueError('The source did not return a media file.')
                extension = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp',
                    'video/mp4': '.mp4', 'video/webm': '.webm', 'audio/mpeg': '.mp3',
                    'audio/mp4': '.m4a'}.get(mime, '.jpg' if mime.startswith('image/') else '.mp4')
                if int(response.headers.get('content-length', 0)) > MAX_MEDIA_BYTES:
                    raise ValueError('The media exceeds the download limit.')
                path = Path(directory) / ('media-' + str(media_index + 1) + extension)
                total = 0
                with path.open('wb') as output:
                    for chunk in response.iter_bytes(1024 * 1024):
                        total += len(chunk)
                        if total > MAX_MEDIA_BYTES or time.monotonic() - started > MAX_DOWNLOAD_SECONDS:
                            raise ValueError('The media exceeds the download limit.')
                        output.write(chunk)
                if not total:
                    raise ValueError('The media download is empty.')
                return path
    raise ValueError('Too many media redirects.')
