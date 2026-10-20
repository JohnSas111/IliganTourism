"""
Developer tool: checks that your Gemini key and model really work.

Run it from the project folder (the one with manage.py):

    py check_gemini.py

It reads GEMINI_API_KEY and GEMINI_MODEL from your .env file (the same values the
website uses), asks Google two simple questions, and explains the answer.
It NEVER prints your key. It contains no secrets, so it is safe to commit.
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE_URL = 'https://generativelanguage.googleapis.com/v1beta'
TIMEOUT_SECONDS = 25


def call(method, url, key, payload=None):
    """Send one request. Returns (http_status, parsed_json_or_None, error_text). Status is 0 if unreachable."""
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={'x-goog-api-key': key, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status if hasattr(response, 'status') else 200, json.loads(response.read().decode('utf-8')), ''
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read().decode('utf-8')), ''
        except Exception:
            return error.code, None, ''
    except (urllib.error.URLError, OSError, ValueError) as error:
        return 0, None, f'{type(error).__name__}: {error}'


def google_reason(body, key):
    """('PERMISSION_DENIED', 'the explanation') from an error reply, with the key hidden."""
    info = body.get('error', {}) if isinstance(body, dict) else {}
    message = ' '.join(str(info.get('message', '')).split())
    if key:
        message = message.replace(key, '[key hidden]')
    return str(info.get('status', '')), message[:400]


def advice(http_status, reason_status, message):
    text = message.lower()
    if http_status == 0:
        return 'Your computer could not reach Google. Check your internet connection, VPN or firewall.'
    if http_status == 400 and ('api key' in text or 'invalid' in text):
        return 'Google does not accept this key. Create a new one at https://aistudio.google.com/apikey and paste it again.'
    if http_status in (401, 403):
        if 'referrer' in text or 'referer' in text:
            return 'The key is restricted to certain websites. Create a new, unrestricted key in Google AI Studio.'
        if 'not been used' in text or 'disabled' in text or 'service_disabled' in text.replace(' ', '_'):
            return ('The Generative Language API is switched off for this key\'s Google project. '
                    'Create the key from https://aistudio.google.com/apikey instead of Google Cloud Console.')
        if 'leaked' in text or 'suspended' in text:
            return 'Google disabled this key. Create a new one, and never share it or commit it.'
        return ('Google refused this key (permission denied). Create a fresh key at https://aistudio.google.com/apikey '
                'and make sure it has no restrictions.')
    if http_status == 404:
        return ('Google does not know the model name in GEMINI_MODEL. Put one of the model names listed above '
                'in your .env as GEMINI_MODEL=... and restart the server.')
    if http_status == 429:
        return ('The free quota for this model is used up, or this model is not on the free tier. '
                'Try another model from the list above (GEMINI_MODEL=...), or wait and try again.')
    if http_status >= 500:
        return 'Google is having trouble right now. Try again in a few minutes.'
    return 'Unexpected answer. Copy the "Google says" line above and send it to your helper.'


def main():
    sys.path.insert(0, os.getcwd())
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'iligan_tourism_system.settings')
    import django
    django.setup()
    from django.conf import settings

    key, model = settings.GEMINI_API_KEY, settings.GEMINI_MODEL
    print('Gemini check')
    print('------------')
    if not key:
        print('GEMINI_API_KEY is empty. Add it to your .env file (GEMINI_API_KEY=your key), then run this again.')
        return 1
    print(f'Key found: yes (length {len(key)}, starts with "AIza": {key.startswith("AIza")})')
    suspicious = [name for name, mark in (('spaces', ' '), ('quote marks', '"'), ("apostrophes", "'"), ('line breaks', '\n'))
                  if mark in key]
    if suspicious:
        print(f'WARNING: the key contains {", ".join(suspicious)}. Re-copy it and paste only the key itself after "=" in .env.')
    print(f'Model setting: {model}')
    print()

    print('1) Asking Google which models this key can see ...')
    status, body, problem = call('GET', f'{BASE_URL}/models?pageSize=200', key)
    available = []
    if status == 200 and isinstance(body, dict):
        available = sorted(m.get('name', '').replace('models/', '') for m in body.get('models', []))
        flash = [name for name in available if 'flash' in name][:30]
        print(f'   OK: the key works. {len(available)} models are visible. Some "flash" models:')
        for name in flash:
            print(f'     - {name}')
        if model in available:
            print(f'   Your GEMINI_MODEL "{model}" is in the list.')
        else:
            print(f'   Your GEMINI_MODEL "{model}" is not in the list (aliases like "-latest" may still work; step 2 will tell).')
    else:
        reason, message = google_reason(body, key)
        print(f'   FAILED (HTTP {status or "no connection"}).')
        print(f'   Google says: {reason} {message}'.rstrip() if (reason or message) else f'   Details: {problem or "no explanation given"}')
        print(f'   What to do: {advice(status, reason, message)}')
        print('\nStopping here, because the key itself is not accepted.')
        return 2
    print()

    print(f'2) Asking the assistant model ({model}) a tiny question ...')
    payload = {
        'model': model,
        'input': 'Reply with exactly one word: ready',
        'generation_config': {'max_output_tokens': 256},
        'store': False,
    }
    if settings.GEMINI_THINKING_LEVEL:
        payload['generation_config']['thinking_level'] = settings.GEMINI_THINKING_LEVEL
    status, body, problem = call('POST', f'{BASE_URL}/interactions', key, payload)
    if status == 200 and isinstance(body, dict):
        answer = ''
        for step in body.get('steps') or []:
            if isinstance(step, dict) and step.get('type') == 'model_output':
                for block in step.get('content') or []:
                    if isinstance(block, dict) and block.get('type') == 'text':
                        answer += block.get('text', '')
        if answer.strip():
            print(f'   OK: the model answered: {answer.strip()[:80]}')
            print('\nRESULT: everything works. Restart "py manage.py runserver" and try the Ask Iligan button.')
            return 0
        print(f'   The request worked but the model returned no text (status: {body.get("status")}).')
        print('   What to do: add GEMINI_THINKING_LEVEL=low to your .env, restart the server and run this again.')
        return 3
    reason, message = google_reason(body, key)
    print(f'   FAILED (HTTP {status or "no connection"}).')
    print(f'   Google says: {reason} {message}'.rstrip() if (reason or message) else f'   Details: {problem or "no explanation given"}')
    print(f'   What to do: {advice(status, reason, message)}')
    return 3


if __name__ == '__main__':
    sys.exit(main())
