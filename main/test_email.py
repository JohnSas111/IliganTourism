"""
Batch 6 tests: booking and payment emails (Brevo).

NOTHING here talks to the internet or sends a real email. Django's test runner replaces the
email backend with an in-memory mailbox (mail.outbox), and the Brevo backend itself is tested
with its network call replaced.

Run only these:   py manage.py test main.test_email
"""
import importlib.util
import io
import json
import os
import subprocess
import sys
import urllib.error
from contextlib import redirect_stdout
from datetime import date, timedelta
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.core import mail
from django.core.mail import EmailMultiAlternatives
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import notifications
from .email_backend import BrevoEmailBackend, BrevoError, build_payload, call_brevo
from .models import EmailLog, EventBooking, Hotel, HotelBooking, Payment, Room, ServiceBooking
from .tests import PASSWORD, BaseCase, fake_paymongo

KEY = 'xkeysib-TESTKEY1234567890'
BREVO = dict(
    BREVO_API_KEY=KEY, BREVO_SENDER_EMAIL='sender@example.com', BREVO_SENDER_NAME='Iligan Tourism',
    DEFAULT_FROM_EMAIL='Iligan Tourism <sender@example.com>', BREVO_TIMEOUT=10, BREVO_API_PROXY='',
)
DUMMY = 'django.core.mail.backends.dummy.EmailBackend'


class FakeReply:
    def __init__(self, body=b'{"messageId": "<abc123@smtp-relay.mailin.fr>"}'):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(status, body=b'{"message": "nope"}'):
    return urllib.error.HTTPError('https://api.brevo.com/v3/smtp/email', status, 'x', {}, io.BytesIO(body))


def sample_message(**extra):
    message = EmailMultiAlternatives('Hello', 'Plain text', to=['customer@example.com'], **extra)
    message.attach_alternative('<p>Hello <b>html</b></p>', 'text/html')
    return message


# ── 1. The Brevo backend (network call replaced) ──────────────────────────

@override_settings(**BREVO)
class BackendTests(SimpleTestCase):

    def send(self, message=None, reply=None, **backend_options):
        seen = {}

        def fake_urlopen(request, timeout=None):
            seen['request'], seen['timeout'] = request, timeout
            if isinstance(reply, BaseException):
                raise reply
            return reply or FakeReply()

        with mock.patch('urllib.request.urlopen', fake_urlopen):
            message = message or sample_message()
            count = BrevoEmailBackend(**backend_options).send_messages([message])
        return count, seen, message

    def test_request_goes_to_the_brevo_endpoint_with_the_key_in_a_header(self):
        count, seen, _ = self.send()
        request = seen['request']
        self.assertEqual(count, 1)
        self.assertEqual(request.full_url, 'https://api.brevo.com/v3/smtp/email')
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(request.get_header('Api-key'), KEY)
        self.assertEqual(request.get_header('Content-type'), 'application/json')
        self.assertEqual(seen['timeout'], 10)

    def test_the_email_is_sent_in_brevos_json_format(self):
        _, seen, _ = self.send()
        body = json.loads(seen['request'].data)
        self.assertEqual(body['sender'], {'name': 'Iligan Tourism', 'email': 'sender@example.com'})
        self.assertEqual(body['to'], [{'email': 'customer@example.com'}])
        self.assertEqual(body['subject'], 'Hello')
        self.assertEqual(body['textContent'], 'Plain text')
        self.assertEqual(body['htmlContent'], '<p>Hello <b>html</b></p>')

    def test_the_key_is_only_ever_in_the_header_never_in_the_body(self):
        _, seen, _ = self.send()
        self.assertNotIn(KEY.encode(), seen['request'].data)
        self.assertNotIn(KEY, seen['request'].full_url)

    def test_reply_to_cc_and_bcc_are_passed_on(self):
        message = sample_message(reply_to=['help@example.com'], cc=['cc@example.com'], bcc=['bcc@example.com'])
        _, seen, _ = self.send(message)
        body = json.loads(seen['request'].data)
        self.assertEqual(body['replyTo'], {'email': 'help@example.com'})
        self.assertEqual(body['cc'], [{'email': 'cc@example.com'}])
        self.assertEqual(body['bcc'], [{'email': 'bcc@example.com'}])

    def test_brevos_message_id_is_kept_on_the_message(self):
        _, _, message = self.send()
        self.assertEqual(message.brevo_message_id, '<abc123@smtp-relay.mailin.fr>')

    def test_a_message_without_recipients_is_not_sent(self):
        empty = EmailMultiAlternatives('x', 'y', to=[])
        count, seen, _ = self.send(empty)
        self.assertEqual(count, 0)
        self.assertNotIn('request', seen)

    def test_brevo_errors_become_friendly_messages(self):
        cases = {
            401: 'did not accept the API key', 403: 'refused the request', 429: 'too many emails',
            400: 'rejected the email', 500: 'error 500',
        }
        for status, expected in cases.items():
            with self.subTest(status), self.assertLogs('main.email_backend', 'ERROR'), \
                    self.assertRaisesMessage(BrevoError, expected):
                self.send(reply=http_error(status))

    def test_brevos_own_reason_is_included_but_the_key_is_hidden(self):
        error = http_error(400, json.dumps({'message': f'Bad sender, key {KEY} seen'}).encode())
        with self.assertLogs('main.email_backend', 'ERROR'), self.assertRaises(BrevoError) as caught:
            self.send(reply=error)
        self.assertIn('Bad sender', str(caught.exception))
        self.assertNotIn(KEY, str(caught.exception))
        self.assertIn('[hidden]', str(caught.exception))

    def test_network_problems_become_a_friendly_message(self):
        for problem in (urllib.error.URLError('no route'), TimeoutError('slow'), OSError('Tunnel connection failed: 403')):
            with self.subTest(type(problem).__name__), self.assertLogs('main.email_backend', 'ERROR'), \
                    self.assertRaisesMessage(BrevoError, 'Could not reach Brevo'):
                self.send(reply=problem)

    def test_an_unreadable_answer_is_an_error_not_a_crash(self):
        with self.assertLogs('main.email_backend', 'ERROR'), self.assertRaisesMessage(BrevoError, 'could not be read'):
            self.send(reply=FakeReply(b'<html>not json</html>'))

    def test_fail_silently_swallows_the_error_and_reports_zero_sent(self):
        with self.assertLogs('main.email_backend', 'ERROR'):
            count, _, _ = self.send(reply=http_error(401), fail_silently=True)
        self.assertEqual(count, 0)

    @override_settings(BREVO_API_PROXY='http://proxy.server:3128')
    def test_the_proxy_setting_routes_the_request_through_the_proxy(self):
        opener = mock.Mock()
        opener.open.return_value = FakeReply()
        with mock.patch('urllib.request.build_opener', return_value=opener) as build, \
                mock.patch('urllib.request.ProxyHandler') as handler, \
                mock.patch('urllib.request.urlopen', side_effect=AssertionError('must use the proxy')):
            BrevoEmailBackend().send_messages([sample_message()])
        handler.assert_called_once_with({'https': 'http://proxy.server:3128', 'http': 'http://proxy.server:3128'})
        build.assert_called_once()
        self.assertEqual(opener.open.call_args.kwargs['timeout'], 10)

    def test_get_requests_have_no_body_and_use_get(self):
        seen = {}

        def fake_urlopen(request, timeout=None):
            seen['request'] = request
            return FakeReply(b'{"senders": []}')

        with mock.patch('urllib.request.urlopen', fake_urlopen):
            answer = call_brevo('https://api.brevo.com/v3/senders')
        self.assertEqual(answer, {'senders': []})
        self.assertEqual(seen['request'].get_method(), 'GET')
        self.assertIsNone(seen['request'].data)
        self.assertEqual(seen['request'].get_header('Api-key'), KEY)

    def test_payload_builder_uses_the_default_sender_when_none_is_given(self):
        self.assertEqual(build_payload(sample_message())['sender']['email'], 'sender@example.com')


# ── 2. Which backend the project picks ────────────────────────────────────

class SettingsTests(SimpleTestCase):

    def probe(self, **env):
        full = dict(os.environ)
        full.update({'DJANGO_SETTINGS_MODULE': 'iligan_tourism_system.settings', 'DEBUG': 'True',
                     'BREVO_API_KEY': '', 'BREVO_SENDER_EMAIL': '', 'BREVO_SENDER_NAME': ''})
        full.update(env)
        if not full['BREVO_SENDER_NAME']:
            del full['BREVO_SENDER_NAME']
        code = ("import django, json; django.setup(); from django.conf import settings as s; "
                "print(json.dumps([s.EMAIL_BACKEND, s.DEFAULT_FROM_EMAIL]))")
        result = subprocess.run([sys.executable, '-c', code], cwd=settings.BASE_DIR, env=full,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_no_settings_means_no_email_is_sent(self):
        self.assertEqual(self.probe()[0], DUMMY)

    def test_a_key_without_a_sender_sends_nothing(self):
        self.assertEqual(self.probe(BREVO_API_KEY=KEY)[0], DUMMY)

    def test_a_sender_without_a_key_sends_nothing(self):
        self.assertEqual(self.probe(BREVO_SENDER_EMAIL='s@example.com')[0], DUMMY)

    def test_key_and_sender_switch_brevo_on(self):
        backend, sender = self.probe(BREVO_API_KEY=KEY, BREVO_SENDER_EMAIL='s@example.com', BREVO_SENDER_NAME='Visit Iligan')
        self.assertEqual(backend, 'main.email_backend.BrevoEmailBackend')
        self.assertEqual(sender, 'Visit Iligan <s@example.com>')

    def test_the_test_runner_never_uses_the_real_backend(self):
        self.assertEqual(settings.EMAIL_BACKEND, 'django.core.mail.backends.locmem.EmailBackend')


# ── 3. The emails the customer gets ───────────────────────────────────────

class EmailCase(BaseCase):
    """Helpers to pay for bookings the same way the existing payment tests do."""

    def pay(self, client, kind):
        today = date.today()
        if kind == 'service':
            client.post(reverse('book_service', args=[self.service.pk]),
                        {'visit_date': str(today + timedelta(days=3)), 'num_guests': 2})
        elif kind == 'event':
            client.post(reverse('book_event', args=[self.event.pk]), {'num_tickets': 2})
        else:
            client.post(reverse('book_room', args=[self.room.pk]), {
                'check_in': str(today + timedelta(days=5)), 'check_out': str(today + timedelta(days=7))})
        with mock.patch('urllib.request.urlopen', fake_paymongo()):
            client.get(reverse('payment_checkout'))
            return client.get(reverse('payment_success'))

    def html_of(self, message):
        return message.alternatives[0][0]

    def log(self, key):
        return EmailLog.objects.get(key=key)


class ConfirmationEmailTests(EmailCase):

    def test_paying_for_a_hotel_room_sends_one_confirmation_with_the_details(self):
        self.pay(self.login(self.customer), 'hotel')
        booking = HotelBooking.objects.get()
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ['c@example.com'])
        self.assertEqual(message.subject, f'Booking confirmed: {booking.reference_number}')
        for text in (message.body, self.html_of(message)):
            for expected in ('Hi customer,', 'Test Hotel', '101', booking.reference_number, '₱2,000.00',
                             'Check-in', 'Check-out', 'within 24 hours'):
                self.assertIn(expected, text)
        self.assertIn(f'{settings.SITE_URL}{reverse("my_bookings")}', message.body)

    def test_paying_for_event_tickets_sends_event_details(self):
        self.pay(self.login(self.customer), 'event')
        message = mail.outbox[0]
        for expected in ('Test Fiesta', 'Plaza', 'Tickets: 2', '₱100.00', EventBooking.objects.get().reference_number):
            self.assertIn(expected, message.body)

    def test_paying_for_an_attraction_service_sends_service_details(self):
        self.pay(self.login(self.customer), 'service')
        message = mail.outbox[0]
        for expected in ('Test Falls', 'Day Pass', 'Guests: 2', '₱200.00', ServiceBooking.objects.get().reference_number):
            self.assertIn(expected, message.body)

    def test_the_email_has_a_plain_text_part_and_an_html_part(self):
        self.pay(self.login(self.customer), 'hotel')
        message = mail.outbox[0]
        self.assertEqual(message.alternatives[0][1], 'text/html')
        self.assertIn('<a href="', self.html_of(message))
        self.assertNotIn('<', message.body)

    def test_the_log_records_the_sent_email(self):
        self.pay(self.login(self.customer), 'hotel')
        row = self.log(f'confirmed:hotel:{HotelBooking.objects.get().pk}')
        self.assertEqual((row.status, row.recipient, row.attempts), ('sent', 'c@example.com', 1))
        self.assertIsNotNone(row.sent_at)

    def test_only_the_customer_gets_it(self):
        self.pay(self.login(self.customer), 'hotel')
        self.assertEqual({address for message in mail.outbox for address in message.recipients()}, {'c@example.com'})

    def test_no_email_when_the_payment_did_not_succeed(self):
        client = self.login(self.customer)
        client.post(reverse('book_event', args=[self.event.pk]), {'num_tickets': 1})
        with mock.patch('urllib.request.urlopen', fake_paymongo()):
            client.get(reverse('payment_checkout'))
        with mock.patch('urllib.request.urlopen', fake_paymongo(status='awaiting_payment_method')):
            client.get(reverse('payment_success'))
        client.get(reverse('payment_cancel'))
        self.assertEqual((len(mail.outbox), EmailLog.objects.count()), (0, 0))

    def test_names_and_text_cannot_inject_html_into_the_email(self):
        User.objects.filter(pk=self.customer.pk).update(first_name='<script>alert(1)</script>', last_name='Tom & Jerry')
        Hotel.objects.filter(pk=self.hotel.pk).update(name='<b>Bold</b> Inn')
        self.pay(self.login(self.customer), 'hotel')
        message = mail.outbox[0]
        html = self.html_of(message)
        self.assertNotIn('<script>', html)
        self.assertNotIn('<b>Bold</b>', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertIn('Tom &amp; Jerry', html)
        self.assertIn('Tom & Jerry', message.body)               # the plain text part stays readable

    def test_the_email_never_contains_secrets(self):
        with override_settings(**BREVO, PAYMONGO_SECRET_KEY='sk_test_SECRETVALUE'):
            self.pay(self.login(self.customer), 'hotel')
        text = mail.outbox[0].body + self.html_of(mail.outbox[0])
        for secret in (KEY, 'sk_test_SECRETVALUE', 'paymongo'):
            self.assertNotIn(secret, text.lower() if secret == 'paymongo' else text)


class NoDuplicateTests(EmailCase):

    def test_repeating_the_same_event_never_sends_a_second_email(self):
        self.pay(self.login(self.customer), 'hotel')
        booking = HotelBooking.objects.get()
        for _ in range(3):
            self.assertIsNone(notifications.booking_confirmed(booking))
        self.assertEqual((len(mail.outbox), EmailLog.objects.count()), (1, 1))

    def test_coming_back_to_the_success_page_again_does_not_resend(self):
        client = self.login(self.customer)
        self.pay(client, 'hotel')
        session = client.session
        session['payment_id'] = Payment.objects.get().pk          # as if the customer reloads the page
        session.save()
        with mock.patch('urllib.request.urlopen', fake_paymongo()):
            client.get(reverse('payment_success'))
        self.assertEqual(HotelBooking.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_cancelling_twice_sends_one_cancellation(self):
        client = self.login(self.customer)
        self.pay(client, 'hotel')
        booking = HotelBooking.objects.get()
        client.post(reverse('cancel_hotel_booking', args=[booking.pk]))
        client.post(reverse('cancel_hotel_booking', args=[booking.pk]))     # refused: already cancelled
        notifications.booking_cancelled(booking)
        self.assertEqual([m.subject.split(':')[0] for m in mail.outbox], ['Booking confirmed', 'Booking cancelled'])

    def test_customer_cancel_then_admin_cancel_is_still_one_cancellation_email(self):
        client = self.login(self.customer)
        self.pay(client, 'event')
        booking = EventBooking.objects.get()
        client.post(reverse('cancel_event_booking', args=[booking.pk]))
        self.login(self.admin).post(reverse('admin_update_event_booking', args=[booking.pk]), {'status': 'cancelled'})
        self.assertEqual(len([m for m in mail.outbox if m.subject.startswith('Booking cancelled')]), 1)

    def test_each_booking_gets_its_own_emails(self):
        client = self.login(self.customer)
        self.pay(client, 'hotel')
        self.pay(client, 'event')
        self.assertEqual(sorted(EmailLog.objects.values_list('key', flat=True)),
                         sorted([f'confirmed:hotel:{HotelBooking.objects.get().pk}',
                                 f'confirmed:event:{EventBooking.objects.get().pk}']))

    def test_the_database_itself_refuses_a_second_row_for_the_same_key(self):
        from django.db import IntegrityError, transaction
        EmailLog.objects.create(key='confirmed:hotel:1', kind='confirmed')
        with self.assertRaises(IntegrityError), transaction.atomic():
            EmailLog.objects.create(key='confirmed:hotel:1', kind='confirmed')


class CancellationEmailTests(EmailCase):

    def test_customer_cancellation_email(self):
        client = self.login(self.customer)
        self.pay(client, 'hotel')
        booking = HotelBooking.objects.get()
        client.post(reverse('cancel_hotel_booking', args=[booking.pk]))
        message = mail.outbox[-1]
        self.assertEqual(message.subject, f'Booking cancelled: {booking.reference_number}')
        self.assertIn('cancelled, as you requested', message.body)
        self.assertIn('Refunds are reviewed and processed by our team', message.body)
        self.assertIn(booking.reference_number, message.body)

    def test_admin_cancellation_email_says_the_team_did_it(self):
        self.pay(self.login(self.customer), 'service')
        booking = ServiceBooking.objects.get()
        self.login(self.admin).post(reverse('admin_update_service_booking', args=[booking.pk]), {'status': 'cancelled'})
        message = mail.outbox[-1]
        self.assertIn('Our team has cancelled your booking', message.body)
        self.assertEqual(message.to, ['c@example.com'])

    def test_admin_setting_the_same_status_again_sends_nothing(self):
        self.pay(self.login(self.customer), 'hotel')
        booking = HotelBooking.objects.get()
        admin = self.login(self.admin)
        for _ in range(3):
            admin.post(reverse('admin_update_hotel_booking', args=[booking.pk]), {'status': 'cancelled'})
        self.assertEqual(len(mail.outbox), 2)                     # confirmation + one cancellation

    def test_marking_a_booking_completed_or_pending_sends_nothing(self):
        self.pay(self.login(self.customer), 'hotel')
        booking = HotelBooking.objects.get()
        admin = self.login(self.admin)
        for status in ('completed', 'pending'):
            admin.post(reverse('admin_update_hotel_booking', args=[booking.pk]), {'status': status})
        self.assertEqual(len(mail.outbox), 1)

    def test_an_invalid_status_sends_nothing(self):
        self.pay(self.login(self.customer), 'hotel')
        self.login(self.admin).post(reverse('admin_update_hotel_booking', args=[HotelBooking.objects.get().pk]),
                                    {'status': 'exploded'})
        self.assertEqual(len(mail.outbox), 1)

    def test_admin_confirming_a_pending_booking_sends_a_staff_confirmation_once(self):
        booking = HotelBooking.objects.create(
            user=self.customer, room=self.room, check_in=date.today() + timedelta(days=4),
            check_out=date.today() + timedelta(days=5), total_price=1000, status='pending', reference_number='HTL-PEND1')
        admin = self.login(self.admin)
        for _ in range(2):
            admin.post(reverse('admin_update_hotel_booking', args=[booking.pk]), {'status': 'confirmed'})
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('Our team has confirmed your booking', mail.outbox[0].body)

    def test_a_cancelled_then_reinstated_booking_does_not_get_a_second_confirmation(self):
        self.pay(self.login(self.customer), 'hotel')
        booking = HotelBooking.objects.get()
        admin = self.login(self.admin)
        admin.post(reverse('admin_update_hotel_booking', args=[booking.pk]), {'status': 'cancelled'})
        admin.post(reverse('admin_update_hotel_booking', args=[booking.pk]), {'status': 'confirmed'})
        self.assertEqual([m.subject.split(':')[0] for m in mail.outbox], ['Booking confirmed', 'Booking cancelled'])

    def test_resaving_the_same_status_on_an_old_booking_sends_nothing(self):
        """Bookings made before emails existed have no log row; re-saving them must stay silent."""
        old = HotelBooking.objects.create(
            user=self.customer, room=self.room, check_in=date.today() + timedelta(days=4),
            check_out=date.today() + timedelta(days=5), total_price=1000, status='confirmed', reference_number='HTL-OLD01')
        gone = HotelBooking.objects.create(
            user=self.customer, room=self.room, check_in=date.today() + timedelta(days=8),
            check_out=date.today() + timedelta(days=9), total_price=1000, status='cancelled', reference_number='HTL-OLD02')
        admin = self.login(self.admin)
        admin.post(reverse('admin_update_hotel_booking', args=[old.pk]), {'status': 'confirmed'})
        admin.post(reverse('admin_update_hotel_booking', args=[gone.pk]), {'status': 'cancelled'})
        self.assertEqual((len(mail.outbox), EmailLog.objects.count()), (0, 0))

    def test_someone_elses_booking_cannot_be_cancelled_so_no_email(self):
        self.pay(self.login(self.customer), 'hotel')
        mail.outbox.clear()
        response = self.login(self.other_customer).post(reverse('cancel_hotel_booking', args=[HotelBooking.objects.get().pk]))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(len(mail.outbox), 0)


class UnfulfilledPaymentEmailTests(EmailCase):

    def test_the_customer_who_paid_but_lost_the_room_is_told_what_to_do(self):
        a, b = self.login(self.customer), self.login(self.other_customer)
        today = date.today()
        form = {'check_in': str(today + timedelta(days=5)), 'check_out': str(today + timedelta(days=7))}
        for client in (a, b):
            client.post(reverse('book_room', args=[self.room.pk]), form)
            with mock.patch('urllib.request.urlopen', fake_paymongo()):
                client.get(reverse('payment_checkout'))
        with mock.patch('urllib.request.urlopen', fake_paymongo()):
            a.get(reverse('payment_success'))
            with self.assertLogs(level='ERROR'):
                b.get(reverse('payment_success'))
        loser = Payment.objects.get(user=self.other_customer)
        message = [m for m in mail.outbox if m.to == ['o@example.com']]
        self.assertEqual(len(message), 1)
        self.assertEqual(message[0].subject, f'Action needed: we could not complete your booking (PAY-{loser.pk})')
        for expected in (f'PAY-{loser.pk}', 'we could not complete your booking', 'booked by someone else',
                         '₱2,000.00', 'refund or rebook'):
            self.assertIn(expected, message[0].body)
        winner = [m for m in mail.outbox if m.to == ['c@example.com']]
        self.assertEqual([m.subject.split(':')[0] for m in winner], ['Booking confirmed'])
        self.assertIsNone(notifications.payment_unfulfilled(loser, 'again'))     # never twice
        self.assertEqual(len(mail.outbox), 2)


class SafetyTests(EmailCase):

    def test_a_customer_without_an_email_address_still_books_and_is_logged_as_skipped(self):
        User.objects.filter(pk=self.customer.pk).update(email='')
        response = self.pay(self.login(self.customer), 'hotel')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(HotelBooking.objects.get().status, 'confirmed')
        self.assertEqual(len(mail.outbox), 0)
        row = EmailLog.objects.get()
        self.assertEqual(row.status, 'skipped')
        self.assertIn('no email address', row.error)

    @override_settings(EMAIL_BACKEND=DUMMY)
    def test_with_email_switched_off_nothing_is_sent_or_logged_and_booking_works(self):
        response = self.pay(self.login(self.customer), 'hotel')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(HotelBooking.objects.count(), 1)
        self.assertEqual((len(mail.outbox), EmailLog.objects.count()), (0, 0))

    def test_a_failing_email_never_breaks_the_booking_or_the_page(self):
        with mock.patch.object(EmailMultiAlternatives, 'send', side_effect=BrevoError('Brevo did not accept the API key.')), \
                self.assertLogs('main.notifications', 'ERROR'):
            response = self.pay(self.login(self.customer), 'hotel')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(HotelBooking.objects.get().status, 'confirmed')
        self.assertEqual(Payment.objects.get().status, 'paid')
        row = EmailLog.objects.get()
        self.assertEqual(row.status, 'failed')
        self.assertIn('did not accept the API key', row.error)

    def test_an_unexpected_error_while_writing_the_email_is_also_contained(self):
        with mock.patch('main.notifications.render_to_string', side_effect=RuntimeError('template exploded')), \
                self.assertLogs('main.notifications', 'ERROR'):
            response = self.pay(self.login(self.customer), 'event')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(EventBooking.objects.get().status, 'confirmed')
        self.assertEqual(EmailLog.objects.get().status, 'failed')

    def test_the_api_key_is_removed_from_stored_error_text(self):
        with override_settings(**BREVO), \
                mock.patch.object(EmailMultiAlternatives, 'send', side_effect=RuntimeError(f'oops {KEY} oops')), \
                self.assertLogs('main.notifications', 'ERROR') as logs:
            self.pay(self.login(self.customer), 'hotel')
        self.assertNotIn(KEY, EmailLog.objects.get().error)
        self.assertNotIn(KEY, '\n'.join(logs.output))

    def test_the_booking_is_saved_before_the_email_is_attempted(self):
        seen = {}

        def spy(message, fail_silently=False):
            seen['booking_exists'] = HotelBooking.objects.filter(status='confirmed').exists()
            return 1

        with mock.patch.object(EmailMultiAlternatives, 'send', spy):
            self.pay(self.login(self.customer), 'hotel')
        self.assertTrue(seen['booking_exists'])

    def test_an_email_for_a_booking_that_no_longer_exists_fails_cleanly(self):
        log = EmailLog.objects.create(key='confirmed:hotel:99999', kind='confirmed')
        with self.assertLogs('main.notifications', 'ERROR'):
            notifications._attempt(log)
        log.refresh_from_db()
        self.assertEqual(log.status, 'failed')
        self.assertIn('DoesNotExist', log.error)


class RetryTests(EmailCase):

    def fail_once(self, kind='hotel'):
        with mock.patch.object(EmailMultiAlternatives, 'send', side_effect=BrevoError('Could not reach Brevo.')), \
                self.assertLogs('main.notifications', 'ERROR'):
            self.pay(self.login(self.customer), kind)
        return EmailLog.objects.get()

    def test_a_failed_email_can_be_sent_later(self):
        failed = self.fail_once()
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(notifications.retry_failed(), (1, 0))
        failed.refresh_from_db()
        self.assertEqual((failed.status, failed.attempts, failed.error), ('sent', 2, ''))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(notifications.retry_failed(), (0, 0))          # nothing left to do
        self.assertEqual(len(mail.outbox), 1)

    def test_retrying_while_brevo_is_still_down_keeps_it_failed(self):
        failed = self.fail_once()
        with mock.patch.object(EmailMultiAlternatives, 'send', side_effect=BrevoError('Could not reach Brevo.')), \
                self.assertLogs('main.notifications', 'ERROR'):
            self.assertEqual(notifications.retry_failed(), (0, 1))
        failed.refresh_from_db()
        self.assertEqual((failed.status, failed.attempts), ('failed', 2))

    def test_an_email_is_given_up_on_after_five_attempts(self):
        failed = self.fail_once()
        EmailLog.objects.filter(pk=failed.pk).update(attempts=notifications.MAX_ATTEMPTS)
        self.assertEqual(notifications.retry_failed(), (0, 0))
        self.assertEqual(len(mail.outbox), 0)

    def test_emails_stuck_in_sending_are_retried_but_fresh_ones_are_left_alone(self):
        self.pay(self.login(self.customer), 'hotel')
        old = EmailLog.objects.get()
        mail.outbox.clear()
        EmailLog.objects.filter(pk=old.pk).update(status='pending', created_at=timezone.now() - timedelta(minutes=30))
        self.assertEqual(notifications.retry_failed(), (1, 0))
        EmailLog.objects.filter(pk=old.pk).update(status='pending', created_at=timezone.now())
        self.assertEqual(notifications.retry_failed(), (0, 0))

    def test_retry_never_resends_an_email_that_was_already_sent(self):
        self.pay(self.login(self.customer), 'hotel')
        notifications.retry_failed()
        self.assertEqual(len(mail.outbox), 1)


# ── 4. Django admin ───────────────────────────────────────────────────────

class AdminTests(EmailCase):

    def setUp(self):
        super().setUp()
        self.client = self.login(User.objects.create_superuser('boss', 'b@example.com', PASSWORD))

    def test_the_email_log_is_listed_and_read_only(self):
        self.pay(self.login(self.customer), 'hotel')
        page = self.client.get(reverse('admin:main_emaillog_changelist'))
        self.assertContains(page, 'confirmed')
        self.assertContains(page, 'c@example.com')
        self.assertEqual(self.client.get(reverse('admin:main_emaillog_add')).status_code, 403)


# ── 5. brevo_tools.py ─────────────────────────────────────────────────────

def load_tools():
    spec = importlib.util.spec_from_file_location('brevo_tools', settings.BASE_DIR / 'brevo_tools.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@override_settings(**BREVO)
class ToolsTests(EmailCase):

    def setUp(self):
        super().setUp()
        from . import email_backend
        self.tools = load_tools()
        self.tools._setup = lambda: (settings, email_backend, notifications)

    def run_tool(self, function, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = function(*args)
        return code, out.getvalue()

    def senders(self, **sender):
        entry = {'id': 1, 'name': 'Iligan Tourism', 'email': 'sender@example.com', 'active': True}
        entry.update(sender)
        return mock.patch('main.email_backend.call_brevo', return_value={'senders': [entry]})

    def test_check_passes_when_the_key_works_and_the_sender_is_verified(self):
        with self.senders():
            code, text = self.run_tool(self.tools.check)
        self.assertEqual(code, 0)
        self.assertIn('RESULT: Brevo is ready', text)
        self.assertIn('sender@example.com is verified', text)
        self.assertNotIn(KEY, text)

    def test_check_explains_an_unverified_sender(self):
        with self.senders(active=False):
            code, text = self.run_tool(self.tools.check)
        self.assertEqual(code, 1)
        self.assertIn('NOT verified', text)

    def test_check_explains_a_sender_that_is_not_on_the_account(self):
        with self.senders(email='someone@else.com'):
            code, text = self.run_tool(self.tools.check)
        self.assertEqual(code, 1)
        self.assertIn('is not in your Brevo senders', text)

    def test_check_matches_the_sender_ignoring_capital_letters(self):
        with self.senders(email='Sender@Example.com'):
            self.assertEqual(self.run_tool(self.tools.check)[0], 0)

    def test_check_shows_brevos_complaint_about_a_bad_key_without_leaking_it(self):
        with mock.patch('main.email_backend.call_brevo', side_effect=BrevoError('Brevo did not accept the API key.')):
            code, text = self.run_tool(self.tools.check)
        self.assertEqual(code, 1)
        self.assertIn('did not accept the API key', text)
        self.assertNotIn(KEY, text)

    @override_settings(BREVO_API_KEY='xsmtpsib-wrongkind')
    def test_check_recognises_an_smtp_key_by_mistake(self):
        code, text = self.run_tool(self.tools.check)
        self.assertEqual(code, 1)
        self.assertIn('SMTP key', text)

    @override_settings(BREVO_API_KEY='', BREVO_SENDER_EMAIL='')
    def test_check_explains_missing_settings(self):
        code, text = self.run_tool(self.tools.check)
        self.assertEqual(code, 1)
        self.assertIn('must both be filled', text)

    def test_check_mentions_the_proxy_when_one_is_set(self):
        with override_settings(BREVO_API_PROXY='http://proxy.server:3128'), self.senders():
            self.assertIn('http://proxy.server:3128', self.run_tool(self.tools.check)[1])

    def test_send_test_sends_one_email_to_the_given_address(self):
        code, text = self.run_tool(self.tools.send_test, 'me@example.com')
        self.assertEqual(code, 0)
        self.assertEqual([m.to for m in mail.outbox], [['me@example.com']])
        self.assertIn('spam folder', text)
        self.assertEqual(EmailLog.objects.count(), 0)               # a test email is not a booking email

    def test_send_test_reports_a_brevo_failure(self):
        with mock.patch.object(EmailMultiAlternatives, 'send', side_effect=BrevoError('Brevo refused the request.')):
            code, text = self.run_tool(self.tools.send_test, 'me@example.com')
        self.assertEqual(code, 1)
        self.assertIn('Brevo refused the request.', text)

    @override_settings(EMAIL_BACKEND=DUMMY)
    def test_send_test_and_retry_refuse_when_email_is_switched_off(self):
        for function, args in ((self.tools.send_test, ('me@example.com',)), (self.tools.retry_failed, ())):
            code, text = self.run_tool(function, *args)
            self.assertEqual(code, 1)
            self.assertIn('switched off', text)

    def test_log_lists_emails_and_their_status(self):
        self.pay(self.login(self.customer), 'hotel')
        code, text = self.run_tool(self.tools.show_log)
        self.assertEqual(code, 0)
        self.assertIn('sent', text)
        self.assertIn('confirmed:hotel:', text)
        self.assertIn('c@example.com', text)

    def test_log_when_empty(self):
        self.assertIn('No notification emails yet', self.run_tool(self.tools.show_log)[1])

    def test_retry_failed_reports_the_outcome(self):
        with mock.patch.object(EmailMultiAlternatives, 'send', side_effect=BrevoError('x')), \
                self.assertLogs('main.notifications', 'ERROR'):
            self.pay(self.login(self.customer), 'hotel')
        code, text = self.run_tool(self.tools.retry_failed)
        self.assertEqual((code, '1 email(s) sent now, 0 still failing.' in text), (0, True))

    def test_command_line_dispatch(self):
        with mock.patch.object(self.tools, 'check', return_value=0) as check, \
                mock.patch.object(self.tools, 'send_test', return_value=0) as send_test, \
                mock.patch.object(self.tools, 'show_log', return_value=0) as show_log, \
                mock.patch.object(self.tools, 'retry_failed', return_value=0) as retry:
            self.tools.main(['x', 'check'])
            self.tools.main(['x', 'send-test', 'a@b.com'])
            self.tools.main(['x', 'log'])
            self.tools.main(['x', 'retry-failed'])
        check.assert_called_once_with()
        send_test.assert_called_once_with('a@b.com')
        show_log.assert_called_once_with()
        retry.assert_called_once_with()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self.tools.main(['x']), 2)
            self.assertEqual(self.tools.main(['x', 'send-test']), 2)


# ── 6. Project wiring ─────────────────────────────────────────────────────

class WiringTests(SimpleTestCase):

    def test_env_example_documents_the_new_settings_without_values(self):
        text = (settings.BASE_DIR / '.env.example').read_text()
        for key in ('BREVO_API_KEY=', 'BREVO_SENDER_EMAIL='):
            self.assertRegex(text, rf'(?m)^{key}\s*$')

    def test_no_api_key_is_hardcoded_in_the_new_code(self):
        for relative in ('main/email_backend.py', 'main/notifications.py', 'brevo_tools.py',
                         'iligan_tourism_system/settings.py', 'templates/emails/message.html', 'templates/emails/message.txt'):
            source = (settings.BASE_DIR / relative).read_text()
            self.assertNotRegex(source, r'xkeysib-[A-Za-z0-9]{12,}')
            self.assertNotRegex(source, r'xsmtpsib-[A-Za-z0-9]{12,}')


class MigrationTests(TestCase):

    def test_the_migration_creates_the_email_log_table(self):
        from django.db import connection
        self.assertIn('main_emaillog', connection.introspection.table_names())
