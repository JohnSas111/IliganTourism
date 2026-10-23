"""
Brevo email backend (Batch 6).

Django sends every email through the backend named in EMAIL_BACKEND. This one delivers
them with Brevo's transactional email API (https://api.brevo.com/v3/smtp/email) instead
of SMTP. It uses only Python's standard library, the same way the PayMongo and Gemini
code does, so no extra package is needed.

Because it is a normal Django backend, Django's test runner swaps it for an in-memory
one automatically: no test can ever send a real email, even if a real key is in .env.
"""
import json
import logging
import urllib.error
import urllib.request
from email.utils import parseaddr

from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend

logger = logging.getLogger(__name__)


class BrevoError(Exception):
    """Brevo did not accept the email. The text is safe to show and to store (no key in it)."""


def _hide_key(text):
    text = str(text)
    key = getattr(settings, 'BREVO_API_KEY', '')
    return text.replace(key, '[hidden]') if key else text


def _friendly_http_error(status, body):
    """Short explanation for an error answer from Brevo."""
    try:
        reason = (json.loads(body).get('message') or '').strip()
    except Exception:
        reason = ''
    reason = _hide_key(reason)[:150]
    if status == 401:
        text = 'Brevo did not accept the API key. Check BREVO_API_KEY (it must start with xkeysib-).'
    elif status == 403:
        text = 'Brevo refused the request (account not activated yet, or this server is not an allowed IP).'
    elif status == 429:
        text = 'Brevo says too many emails were sent. Try again later.'
    elif status == 400:
        text = 'Brevo rejected the email.'
    else:
        text = f'Brevo answered with error {status}.'
    return f'{text} ({reason})' if reason else text


def open_url(request):
    """Send a prepared request, through BREVO_API_PROXY when one is set."""
    timeout = int(getattr(settings, 'BREVO_TIMEOUT', 10))
    proxy = (getattr(settings, 'BREVO_API_PROXY', '') or '').strip()
    if proxy:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({'https': proxy, 'http': proxy}))
        return opener.open(request, timeout=timeout)
    return urllib.request.urlopen(request, timeout=timeout)


def call_brevo(url, payload=None):
    """GET (no payload) or POST (JSON payload) to Brevo. Returns the decoded JSON answer."""
    headers = {
        'api-key': settings.BREVO_API_KEY,
        'accept': 'application/json',
        'user-agent': 'IliganTourism/1.0',
    }
    data = None
    if payload is not None:
        headers['content-type'] = 'application/json'
        data = json.dumps(payload).encode('utf-8')
    request = urllib.request.Request(url, data=data, headers=headers, method='POST' if payload is not None else 'GET')
    try:
        with open_url(request) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise BrevoError(_friendly_http_error(exc.code, exc.read())) from None
    except Exception as exc:
        raise BrevoError(f'Could not reach Brevo ({type(exc).__name__}). Check the internet connection '
                         'or, on a free PythonAnywhere account, BREVO_API_PROXY.') from None
    try:
        return json.loads(body) if body else {}
    except ValueError:
        raise BrevoError('Brevo sent an answer that could not be read.') from None


def _address(value):
    name, email = parseaddr(value)
    return {'email': email, **({'name': name} if name else {})}


def build_payload(message):
    """Turn a Django EmailMessage into Brevo's JSON format."""
    payload = {
        'sender': _address(message.from_email or settings.DEFAULT_FROM_EMAIL),
        'to': [_address(recipient) for recipient in message.to],
        'subject': message.subject,
        'textContent': message.body,
    }
    for content, mimetype in getattr(message, 'alternatives', []):
        if mimetype == 'text/html':
            payload['htmlContent'] = content
    if message.reply_to:
        payload['replyTo'] = _address(message.reply_to[0])
    if message.cc:
        payload['cc'] = [_address(recipient) for recipient in message.cc]
    if message.bcc:
        payload['bcc'] = [_address(recipient) for recipient in message.bcc]
    return payload


class BrevoEmailBackend(BaseEmailBackend):

    def send_messages(self, email_messages):
        sent = 0
        for message in email_messages:
            if not message.recipients():
                continue
            try:
                answer = call_brevo(settings.BREVO_API_URL, build_payload(message))
            except BrevoError as exc:
                logger.error('Brevo email failed: %s', exc)
                if self.fail_silently:
                    continue
                raise
            message.brevo_message_id = str(answer.get('messageId', ''))[:200]
            sent += 1
        return sent
