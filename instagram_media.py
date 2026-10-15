"""Keep Instagram photos alongside videos in yt-dlp post results."""

from yt_dlp.extractor.instagram import InstagramIE
from yt_dlp.utils import determine_ext


class InstagramMediaIE(InstagramIE):
    @classmethod
    def ie_key(cls):
        # Replace the built-in extractor so nested carousel results use it too.
        return InstagramIE.ie_key()

    def _extract_product_media(self, product_media):
        info = super()._extract_product_media(product_media)
        if product_media.get('media_type') != 1 or info.get('formats'):
            return info
        images = info.get('thumbnails') or []
        if not images:
            return info
        # yt-dlp orders thumbnails best-last. Preserve that preference when
        # Instagram omits dimensions instead of choosing the smallest preview.
        image = max(reversed(images), key=_image_area)
        return {**info, 'formats': [{
            'url': image['url'],
            'ext': determine_ext(image['url'], default_ext='jpg'),
            'width': image.get('width'),
            'height': image.get('height'),
        }]}


def _image_area(image):
    try:
        return max(0, int(image.get('width') or 0)) * max(0, int(image.get('height') or 0))
    except (TypeError, ValueError):
        return 0
