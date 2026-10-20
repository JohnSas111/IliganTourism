"""
Outside-world helpers for maps and weather (Batch 3).

* geocode()        - turns an address into coordinates using OpenStreetMap Nominatim.
* fetch_forecast() - gets a 16-day forecast from the Open-Meteo API.

Both are best-effort: on any network or data problem they return None, and the
rest of the site keeps working. Neither needs an API key.

Usage rules we follow:
* Nominatim: at most 1 request/second, and an identifying User-Agent
  (set GEO_USER_AGENT in .env). We only call it when an admin saves a place
  without coordinates, so traffic is tiny.
* Open-Meteo: free for non-commercial use. Results are cached for 30 minutes so
  page views do not hit the API every time.
"""
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

NOMINATIM_URL = 'https://nominatim.openstreetmap.org/search'
OPEN_METEO_URL = 'https://api.open-meteo.com/v1/forecast'
TIMEOUT_SECONDS = 6
NOMINATIM_PAUSE_SECONDS = 1.1     # Nominatim's policy: at most one request per second
FORECAST_CACHE_SECONDS = 30 * 60

# Errors that mean "the outside service is unavailable or answered nonsense".
_EXPECTED_ERRORS = (urllib.error.URLError, OSError, ValueError, KeyError, TypeError, IndexError)

# WMO weather codes used by Open-Meteo -> (description, icon)
WEATHER_CODES = {
    0: ('Clear sky', '☀️'), 1: ('Mainly clear', '🌤️'), 2: ('Partly cloudy', '⛅'), 3: ('Overcast', '☁️'),
    45: ('Fog', '🌫️'), 48: ('Fog', '🌫️'),
    51: ('Light drizzle', '🌦️'), 53: ('Drizzle', '🌦️'), 55: ('Heavy drizzle', '🌧️'),
    56: ('Freezing drizzle', '🌧️'), 57: ('Freezing drizzle', '🌧️'),
    61: ('Light rain', '🌦️'), 63: ('Rain', '🌧️'), 65: ('Heavy rain', '🌧️'),
    66: ('Freezing rain', '🌧️'), 67: ('Freezing rain', '🌧️'),
    71: ('Light snow', '🌨️'), 73: ('Snow', '🌨️'), 75: ('Heavy snow', '🌨️'), 77: ('Snow grains', '🌨️'),
    80: ('Light showers', '🌦️'), 81: ('Showers', '🌧️'), 82: ('Violent showers', '🌧️'),
    85: ('Snow showers', '🌨️'), 86: ('Snow showers', '🌨️'),
    95: ('Thunderstorm', '⛈️'), 96: ('Thunderstorm with hail', '⛈️'), 99: ('Thunderstorm with hail', '⛈️'),
}


def _get_json(url, params):
    full_url = f'{url}?{urllib.parse.urlencode(params)}'
    request = urllib.request.Request(full_url, headers={
        'User-Agent': settings.GEO_USER_AGENT,
        'Accept': 'application/json',
    })
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode('utf-8'))


def _lookup(place):
    """
    Ask Nominatim about one place. Returns (status, value):
      ('found', (lat, lon))  - a match
      ('none', None)         - the service answered but knows no such place
      ('error', None)        - the service could not be reached or answered nonsense
    """
    query = str(place).strip()
    if 'iligan' not in query.lower():
        query += ', Iligan City, Philippines'
    try:
        results = _get_json(NOMINATIM_URL, {
            'q': query, 'format': 'jsonv2', 'limit': 1, 'countrycodes': 'ph'})
        if not results:
            logger.warning('Geocoding found no match for %r', query)
            return 'none', None
        return 'found', (str(results[0]['lat']), str(results[0]['lon']))
    except _EXPECTED_ERRORS as error:
        logger.warning('Geocoding failed for %r: %s', query, error)
        return 'error', None


def geocode(place):
    """Return (latitude, longitude) as strings for a place name/address, or None."""
    if not place or not str(place).strip():
        return None
    return _lookup(place)[1]


def geocode_any(places):
    """
    Try several wordings, most specific first, and stop at the first match.
    Returns (latitude, longitude, wording_that_matched) or None.

    Waits between attempts (Nominatim allows 1 request/second) and gives up
    straight away if the service itself is unreachable, so saving a place never hangs.
    """
    tried = set()
    first_attempt = True
    for place in places:
        text = ' '.join(str(place or '').split())
        if not text or text.lower() in tried:
            continue
        tried.add(text.lower())
        if not first_attempt:
            time.sleep(NOMINATIM_PAUSE_SECONDS)
        first_attempt = False
        status, value = _lookup(text)
        if status == 'found':
            return value[0], value[1], text
        if status == 'error':
            return None
    return None


def _describe(code):
    return WEATHER_CODES.get(code, ('Unknown', '❓'))


def _whole(value):
    return None if value is None else round(value)


def _normalise(data):
    """Reduce Open-Meteo's response to just what the pages need."""
    current = data['current']
    summary, icon = _describe(current['weather_code'])
    daily = data['daily']
    days = []
    for index, day in enumerate(daily['time']):
        day_summary, day_icon = _describe(daily['weather_code'][index])
        parsed = date.fromisoformat(day)
        days.append({
            'date': day,
            'label': f'{parsed:%a}, {parsed:%b} {parsed.day}',
            'summary': day_summary,
            'icon': day_icon,
            'tmax': _whole(daily['temperature_2m_max'][index]),
            'tmin': _whole(daily['temperature_2m_min'][index]),
            'rain': daily['precipitation_probability_max'][index],
        })
    return {
        'current': {'temperature': _whole(current['temperature_2m']), 'summary': summary, 'icon': icon},
        'days': days,
    }


def fetch_forecast(latitude, longitude):
    """Return {'current': {...}, 'days': [...]} for a location, or None if unavailable."""
    lat, lng = round(float(latitude), 3), round(float(longitude), 3)
    cache_key = f'forecast:{lat}:{lng}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    try:
        data = _get_json(OPEN_METEO_URL, {
            'latitude': lat,
            'longitude': lng,
            'current': 'temperature_2m,weather_code',
            'daily': 'weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max',
            'timezone': 'Asia/Manila',
            'forecast_days': 16,
        })
        forecast = _normalise(data)
    except _EXPECTED_ERRORS as error:
        logger.warning('Forecast request failed for %s,%s: %s', lat, lng, error)
        return None
    cache.set(cache_key, forecast, FORECAST_CACHE_SECONDS)
    return forecast