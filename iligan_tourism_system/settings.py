from pathlib import Path
from decouple import config, Csv
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

# All secrets and per-environment values come from environment variables
# (a local .env file, or PythonAnywhere's WSGI/config). See .env.example.
DEBUG = config('DEBUG', default=False, cast=bool)

# Django's own signing key. Must be set when DEBUG is off.
SECRET_KEY = config('DJANGO_SECRET_KEY', default='')
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = 'local-development-only-key-not-for-production'
    else:
        raise ImproperlyConfigured(
            'DJANGO_SECRET_KEY is not set. Set it as an environment variable '
            '(see .env.example) before running with DEBUG off.'
        )

# Comma-separated list, e.g. "yourname.pythonanywhere.com"
ALLOWED_HOSTS = config('ALLOWED_HOSTS', default='127.0.0.1,localhost', cast=Csv())
CSRF_TRUSTED_ORIGINS = config('CSRF_TRUSTED_ORIGINS', default='', cast=Csv())

# Cookies are only sent over HTTPS when not in DEBUG. Set SECURE_COOKIES=False
# only if you run DEBUG=False locally over plain http.
SECURE_COOKIES = config('SECURE_COOKIES', default=not DEBUG, cast=bool)
SESSION_COOKIE_SECURE = SECURE_COOKIES
CSRF_COOKIE_SECURE = SECURE_COOKIES

# Django's default ('same-origin') tells browsers not to say which site a request came from.
# OpenStreetMap's map tiles refuse requests without that information, so send the site's
# address (not the full page URL) to other sites.
SECURE_REFERRER_POLICY = 'strict-origin-when-cross-origin'

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'main',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'iligan_tourism_system.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'iligan_tourism_system.wsgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
        'OPTIONS': {
            # Take the write lock when a transaction starts, so the availability
            # re-check and the booking insert cannot interleave with another request.
            # (Requires Django 5.1+.)
            'transaction_mode': 'IMMEDIATE',
            'timeout': 20,
        },
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Manila'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'   # target of `collectstatic` on PythonAnywhere
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/'

# Media files
import os
MEDIA_URL = '/media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

# ── PayMongo Settings ──────────────────────────────
# Get your keys from https://dashboard.paymongo.com/developers
# Use test keys (pk_test_... / sk_test_...) for testing
# Use live keys (pk_live_... / sk_live_...) for real payments
PAYMONGO_PUBLIC_KEY  = config('PAYMONGO_PUBLIC_KEY', default='')
# New name is PAYMONGO_SECRET_KEY. The old environment variable name
# (SECRET_KEY) is still accepted as a fallback so existing setups keep working.
# Remove the fallback once PythonAnywhere has been updated.
PAYMONGO_SECRET_KEY  = config('PAYMONGO_SECRET_KEY', default='') or config('SECRET_KEY', default='')
PAYMONGO_API_URL     = 'https://api.paymongo.com/v1'

# The base URL of your site — used for PayMongo redirect after payment
# Set SITE_URL in the environment when deploying, e.g. https://yourname.pythonanywhere.com
SITE_URL = config('SITE_URL', default='http://127.0.0.1:8000').rstrip('/')