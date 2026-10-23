"""
Developer tool for the Brevo email integration (Batch 6).

Run it from the project folder (the one with manage.py):

    py brevo_tools.py check                    is the key valid, is the sender verified, can we reach Brevo?
    py brevo_tools.py send-test you@mail.com   send ONE real test email to that address
    py brevo_tools.py log                      show the last 15 notification emails and their status
    py brevo_tools.py retry-failed             send again every email that failed

It reads the same .env values as the website. It NEVER prints your API key.
It contains no secrets, so it is safe to commit.
"""
import os
import sys

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'iligan_tourism_system.settings')


def _setup():
    import django
    django.setup()
    from django.conf import settings
    from main import email_backend, notifications
    return settings, email_backend, notifications


def check():
    settings, backend, notifications = _setup()
    key, sender = settings.BREVO_API_KEY, settings.BREVO_SENDER_EMAIL

    print(f'API key set:   {"yes" if key else "NO"}')
    print(f'Sender email:  {sender or "NOT SET"}')
    print(f'Proxy:         {settings.BREVO_API_PROXY or "none"}'
          + ('' if settings.BREVO_API_PROXY else '  (free PythonAnywhere? you may need BREVO_API_PROXY=http://proxy.server:3128)'))
    if not key or not sender:
        print('FAIL: BREVO_API_KEY and BREVO_SENDER_EMAIL must both be filled in your .env file.')
        return 1
    if key.startswith('xsmtpsib-'):
        print('FAIL: that is an SMTP key. Create an API key (it starts with xkeysib-) under SMTP & API > API keys.')
        return 1
    if not key.startswith('xkeysib-'):
        print('WARNING: an API key normally starts with "xkeysib-". Double-check you copied the right one.')

    print('1/2 Asking Brevo for your senders (this also tests the key) ...')
    try:
        answer = backend.call_brevo('https://api.brevo.com/v3/senders')
    except backend.BrevoError as error:
        print(f'FAIL: {error}')
        return 1
    senders = answer.get('senders') or []
    print(f'      OK, Brevo accepted the key. {len(senders)} sender(s) on the account.')

    print('2/2 Looking for your sender ...')
    match = next((s for s in senders if (s.get('email') or '').lower() == sender.lower()), None)
    if match is None:
        print(f'FAIL: {sender} is not in your Brevo senders. Add it under Senders, domains & dedicated IPs > Senders.')
        return 1
    if not match.get('active'):
        print(f'FAIL: {sender} exists but is NOT verified yet. Open the confirmation email Brevo sent to it.')
        return 1
    print(f'      OK, {sender} is verified.')
    print('RESULT: Brevo is ready. Now run: py brevo_tools.py send-test <your email address>')
    return 0


def send_test(address):
    settings, backend, notifications = _setup()
    from django.core.mail import EmailMultiAlternatives
    if not notifications.email_enabled():
        print('FAIL: email is switched off. Fill BREVO_API_KEY and BREVO_SENDER_EMAIL in .env first.')
        return 1
    message = EmailMultiAlternatives(
        'Iligan Tourism test email',
        'If you can read this, booking emails work.\n\nIligan Tourism',
        to=[address])
    message.attach_alternative('<p>If you can read this, <strong>booking emails work</strong>.</p><p>Iligan Tourism</p>',
                               'text/html')
    try:
        message.send(fail_silently=False)
    except backend.BrevoError as error:
        print(f'FAIL: {error}')
        return 1
    print(f'OK: Brevo accepted the email for {address} (message id {getattr(message, "brevo_message_id", "?")}).')
    print('Now check that inbox, and the spam folder too.')
    return 0


def show_log():
    _setup()
    from main.models import EmailLog
    rows = list(EmailLog.objects.order_by('-created_at')[:15])
    if not rows:
        print('No notification emails yet.')
        return 0
    for row in rows:
        when = row.created_at.strftime('%Y-%m-%d %H:%M')
        print(f'{when}  {row.status:8}  {row.key:28}  {row.recipient or "(no address)"}'
              + (f'  <- {row.error}' if row.error else ''))
    return 0


def retry_failed():
    _setup()
    from main import notifications
    if not notifications.email_enabled():
        print('FAIL: email is switched off. Fill BREVO_API_KEY and BREVO_SENDER_EMAIL in .env first.')
        return 1
    sent, failed = notifications.retry_failed()
    print(f'{sent} email(s) sent now, {failed} still failing.')
    return 1 if failed else 0


def main(argv):
    command = argv[1] if len(argv) > 1 else ''
    if command == 'check':
        return check()
    if command == 'send-test' and len(argv) > 2:
        return send_test(argv[2])
    if command == 'log':
        return show_log()
    if command == 'retry-failed':
        return retry_failed()
    print(__doc__)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv))
