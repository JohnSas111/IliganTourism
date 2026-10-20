"""
Tourism image storage (Batch 5).

Images of attractions, hotels, rooms and events are uploaded to Cloudinary when the
three CLOUDINARY_* settings are filled in. When they are not, everything keeps working
exactly as before: files are saved in the local ``media/`` folder.

How the two worlds live side by side
------------------------------------
* A NEW upload to Cloudinary is stored in the database as ``cld/<public id>``
  (for example ``cld/iligan_tourism/attractions/timoga_1a2b3c4d``).
* An OLD image keeps its old name (for example ``attractions/timoga.png``) and is still
  served from ``media/``. Nothing existing breaks.
* ``image.url`` in every template therefore keeps working for both kinds.

Only the Cloudinary *upload* and *delete* calls leave your server. The API secret is sent
to nobody: the SDK uses it to sign the request, and only the signature travels.
"""
import io
import logging
import os
import re
import secrets

from django.conf import settings
from django.core.files.storage import FileSystemStorage

logger = logging.getLogger(__name__)

CLOUD_PREFIX = 'cld/'
ALLOWED_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp', '.gif')
ALLOWED_PILLOW_FORMATS = ('JPEG', 'PNG', 'WEBP', 'GIF')
ALLOWED_CLOUDINARY_FORMATS = ['jpg', 'png', 'webp', 'gif']
FORMAT_HELP = 'JPG, PNG, WebP or GIF'


class ImageValidationError(ValueError):
    """The chosen file is not an acceptable image. The text is safe to show to the admin."""


class ImageStorageError(Exception):
    """The image could not be stored. The text is safe to show to the admin."""


# ── Settings helpers ──────────────────────────────────────────────────────

def cloudinary_status():
    """'ready' (all three values set), 'partial' (some set) or 'off' (none set)."""
    values = [
        getattr(settings, 'CLOUDINARY_CLOUD_NAME', ''),
        getattr(settings, 'CLOUDINARY_API_KEY', ''),
        getattr(settings, 'CLOUDINARY_API_SECRET', ''),
    ]
    filled = sum(1 for value in values if value)
    if filled == 3:
        return 'ready'
    return 'partial' if filled else 'off'


def cloudinary_ready():
    return cloudinary_status() == 'ready'


def is_cloud_name(name):
    return bool(name) and str(name).startswith(CLOUD_PREFIX)


def max_upload_bytes():
    return int(settings.IMAGE_MAX_UPLOAD_MB) * 1024 * 1024


def _credentials():
    return {
        'cloud_name': settings.CLOUDINARY_CLOUD_NAME,
        'api_key': settings.CLOUDINARY_API_KEY,
        'api_secret': settings.CLOUDINARY_API_SECRET,
    }


def _hide_secrets(text):
    """Remove the API key and secret from any text before it is logged or shown."""
    text = str(text)
    for secret in (settings.CLOUDINARY_API_SECRET, settings.CLOUDINARY_API_KEY):
        if secret:
            text = text.replace(secret, '[hidden]')
    return text


# ── Validation ────────────────────────────────────────────────────────────

def validate_image_upload(upload):
    """
    Check an uploaded file before it is stored. Raises ImageValidationError.

    Checks the size, the file extension AND the real content (the file must really be
    a JPEG, PNG, WebP or GIF picture that is not absurdly large). SVG and other types
    are refused on purpose: an SVG can carry scripts.
    """
    from PIL import Image

    name = getattr(upload, 'name', '') or 'The file'
    size = getattr(upload, 'size', None)
    limit_mb = int(settings.IMAGE_MAX_UPLOAD_MB)

    if size is not None and size <= 0:
        raise ImageValidationError(f'"{name}" is empty.')
    if size is not None and size > max_upload_bytes():
        raise ImageValidationError(
            f'"{name}" is {size / (1024 * 1024):.1f} MB. The limit is {limit_mb} MB. '
            'Please choose a smaller picture.')
    if os.path.splitext(name)[1].lower() not in ALLOWED_EXTENSIONS:
        raise ImageValidationError(f'"{name}" is not a {FORMAT_HELP} file.')

    try:
        upload.seek(0)
        with Image.open(upload) as picture:
            picture_format = picture.format
            width, height = picture.size
            picture.verify()
    except Exception:
        raise ImageValidationError(f'"{name}" is not a readable picture. Please choose another file.')
    finally:
        try:
            upload.seek(0)
        except Exception:
            pass

    if picture_format not in ALLOWED_PILLOW_FORMATS:
        raise ImageValidationError(f'"{name}" is not a {FORMAT_HELP} picture.')
    max_megapixels = int(settings.IMAGE_MAX_MEGAPIXELS)
    if width * height > max_megapixels * 1_000_000:
        raise ImageValidationError(
            f'"{name}" is {width}x{height} pixels (more than {max_megapixels} megapixels). '
            'Please choose a smaller picture.')


# ── Cloudinary calls ──────────────────────────────────────────────────────

def _proxy_address():
    """
    Address of the outgoing-traffic proxy, or ''. FREE PythonAnywhere accounts can only
    reach the internet through http://proxy.server:3128, and Cloudinary's library does not
    notice that by itself (the symptom is "Connection refused"). Set CLOUDINARY_API_PROXY
    in .env / the hosting settings; the standard HTTPS_PROXY variable is used as a fallback.
    """
    return (getattr(settings, 'CLOUDINARY_API_PROXY', '')
            or os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy') or '').strip()


def _uploader():
    """cloudinary.uploader, set up to use the proxy when one is configured."""
    import cloudinary
    import cloudinary.uploader as uploader  # imported here so the project also runs without the package
    import cloudinary.utils

    proxy = _proxy_address()
    if proxy != getattr(uploader, '_iligan_proxy', ''):
        cloudinary.config(api_proxy=proxy or None)
        # The library builds its connection pool once, when it is first imported: rebuild it.
        uploader._http = cloudinary.utils.get_http_connector(cloudinary.config(), cloudinary.CERT_KWARGS)
        uploader._iligan_proxy = proxy
    return uploader


def _friendly_upload_error(exc):
    text = _hide_secrets(exc).lower()
    if 'signature' in text or 'api_key' in text or 'api key' in text or 'cloud_name' in text \
            or 'unknown api' in text or 'must supply' in text or 'authorization' in text:
        return ('Cloudinary did not accept the account details. Check CLOUDINARY_CLOUD_NAME, '
                'CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET in your .env file.')
    if 'too large' in text or 'file size' in text or 'megapixel' in text:
        return 'Cloudinary refused the picture because it is too large for the free plan.'
    if 'rate limit' in text or 'too many' in text:
        return 'Cloudinary is busy (rate limit reached). Please try again in a few minutes.'
    if 'socket' in text or 'unexpected error' in text or 'timed out' in text or 'connection' in text:
        return 'Could not reach Cloudinary. Check the internet connection and try again.'
    return 'Cloudinary could not store the picture. Try again; if it keeps failing, see the server log.'


def _make_public_id(name):
    """Turn 'attractions/My Photo.PNG' into 'iligan_tourism/attractions/My-Photo_1a2b3c4d'."""
    folder, _, filename = str(name).replace('\\', '/').rpartition('/')
    stem = os.path.splitext(filename)[0]
    stem = re.sub(r'[^A-Za-z0-9_-]+', '-', stem).strip('-_')[:30] or 'image'
    folder = re.sub(r'[^A-Za-z0-9_/-]+', '-', folder).strip('/-_')[:25]
    root = re.sub(r'[^A-Za-z0-9_-]+', '-', getattr(settings, 'CLOUDINARY_FOLDER', '') or '').strip('-_')
    parts = [part for part in (root, folder) if part]
    parts.append(f'{stem}_{secrets.token_hex(4)}')
    return '/'.join(parts)


def delivery_url(name):
    """The public address of a Cloudinary image (resized down to a sensible maximum)."""
    cloud = getattr(settings, 'CLOUDINARY_CLOUD_NAME', '')
    if not cloud:
        return ''
    public_id = str(name)[len(CLOUD_PREFIX):]
    transform = (getattr(settings, 'CLOUDINARY_DELIVERY_TRANSFORM', '') or '').strip('/')
    middle = f'{transform}/' if transform else ''
    return f'https://res.cloudinary.com/{cloud}/image/upload/{middle}{public_id}'


class HybridMediaStorage(FileSystemStorage):
    """Django's default file storage: Cloudinary for new images, local disk for old ones."""

    def _save(self, name, content):
        if not cloudinary_ready():
            return super()._save(name, content)

        public_id = _make_public_id(name)
        try:
            content.seek(0)
            data = content.read()
            result = _uploader().upload(
                io.BytesIO(data),
                public_id=public_id,
                resource_type='image',
                type='upload',
                overwrite=False,
                unique_filename=False,
                allowed_formats=ALLOWED_CLOUDINARY_FORMATS,
                filename=os.path.basename(str(name)) or 'upload',
                timeout=int(getattr(settings, 'CLOUDINARY_TIMEOUT', 30)),
                **_credentials(),
            )
        except Exception as exc:
            logger.error('Cloudinary upload failed (%s): %s', type(exc).__name__, _hide_secrets(exc))
            raise ImageStorageError(_friendly_upload_error(exc)) from None

        stored_id = (result or {}).get('public_id') or public_id
        return CLOUD_PREFIX + stored_id

    def url(self, name):
        if is_cloud_name(name):
            return delivery_url(name)
        return super().url(name)

    def exists(self, name):
        if is_cloud_name(name):
            return True
        return super().exists(name)

    def delete(self, name):
        if not is_cloud_name(name):
            return super().delete(name)
        if not cloudinary_ready():
            logger.warning('Cloudinary is not configured, so %s was not deleted there.', name)
            return
        try:
            _uploader().destroy(
                name[len(CLOUD_PREFIX):], resource_type='image', type='upload', invalidate=True,
                timeout=int(getattr(settings, 'CLOUDINARY_TIMEOUT', 30)), **_credentials())
        except Exception as exc:  # cleaning up must never break the page
            logger.warning('Could not delete %s from Cloudinary (%s): %s',
                           name, type(exc).__name__, _hide_secrets(exc))

    def size(self, name):
        if is_cloud_name(name):
            return 0
        return super().size(name)

    def path(self, name):
        if is_cloud_name(name):
            raise NotImplementedError('Cloudinary images have no local path.')
        return super().path(name)

    def _open(self, name, mode='rb'):
        if is_cloud_name(name):
            raise FileNotFoundError('Cloudinary images are not stored on this server.')
        return super()._open(name, mode)
