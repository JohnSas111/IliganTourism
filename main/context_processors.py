from django.conf import settings


def assistant(request):
    """Lets templates show the AI assistant button only when a Gemini key is configured."""
    return {'assistant_enabled': bool(settings.GEMINI_API_KEY)}