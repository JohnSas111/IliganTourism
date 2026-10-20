from django.conf import settings

from .cloud_storage import cloudinary_status


def assistant(request):
    """Lets templates show the AI assistant button only when a Gemini key is configured."""
    return {'assistant_enabled': bool(settings.GEMINI_API_KEY)}


def image_storage(request):
    """Tells the admin image forms where pictures are stored and which limits apply."""
    status = cloudinary_status()
    label = {
        'ready': 'Cloudinary',
        'partial': 'this server (Cloudinary settings are incomplete)',
    }.get(status, 'this server')
    return {
        'image_storage_label': label,
        'image_max_mb': settings.IMAGE_MAX_UPLOAD_MB,
    }
