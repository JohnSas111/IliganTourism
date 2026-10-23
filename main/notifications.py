"""
Booking and payment emails (Batch 6).

Four situations send an email to the customer:

  * booking_confirmed(booking)           payment received and booking created
                                         (also: an admin sets a booking to "confirmed")
  * booking_cancelled(booking)           the customer cancelled
                                         (by_staff=True: an admin set it to "cancelled")
  * payment_unfulfilled(payment, reason) the customer paid but the room/tickets were taken

Rules that make this safe:

  * NEVER raises. A problem with email can not break a booking, a payment or a page.
  * NO DUPLICATES. Before sending, a row with a unique key (for example
    "confirmed:hotel:12") is written to EmailLog. A repeat of the same event finds the key
    taken and sends nothing. One confirmation and one cancellation per booking, at most.
  * Nothing is sent (and nothing is logged) when email is switched off, i.e. when no Brevo
    key and sender are configured.
  * A failed email stays in EmailLog as "failed" and can be sent again with
    "py brevo_tools.py retry-failed".
"""
import logging
from datetime import date, time, timedelta
from decimal import Decimal

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.dateformat import format as format_date

from .email_backend import _hide_key
from .models import (
    EmailLog, EventBooking, HotelBooking, Payment, ServiceBooking,
)

logger = logging.getLogger(__name__)

KIND_CONFIRMED = 'confirmed'
KIND_CANCELLED = 'cancelled'
KIND_UNFULFILLED = 'unfulfilled'
MAX_ATTEMPTS = 5
BOOKING_MODELS = {'hotel': HotelBooking, 'event': EventBooking, 'service': ServiceBooking}


# ── Public functions ──────────────────────────────────────────────────────

def email_enabled():
    """False when no Brevo key + sender are set (the project then uses the 'dummy' backend)."""
    return settings.EMAIL_BACKEND != 'django.core.mail.backends.dummy.EmailBackend'


def booking_type(booking):
    for name, model in BOOKING_MODELS.items():
        if isinstance(booking, model):
            return name
    raise ValueError(f'Not a booking: {booking!r}')


def booking_confirmed(booking, by_staff=False):
    return _deliver(f'{KIND_CONFIRMED}:{booking_type(booking)}:{booking.pk}', KIND_CONFIRMED,
                    detail='staff' if by_staff else '')


def booking_cancelled(booking, by_staff=False):
    return _deliver(f'{KIND_CANCELLED}:{booking_type(booking)}:{booking.pk}', KIND_CANCELLED,
                    detail='staff' if by_staff else '')


def payment_unfulfilled(payment, reason=''):
    return _deliver(f'{KIND_UNFULFILLED}:payment:{payment.pk}', KIND_UNFULFILLED, detail=str(reason)[:300])


def retry_failed():
    """Send again every email that failed (or got stuck). Returns (now_sent, still_failed)."""
    stuck = timezone.now() - timedelta(minutes=10)
    waiting = EmailLog.objects.filter(attempts__lt=MAX_ATTEMPTS).filter(
        Q(status='failed') | Q(status='pending', created_at__lt=stuck))
    now_sent = still_failed = 0
    for log in waiting.order_by('pk'):
        _attempt(log)
        if log.status == 'sent':
            now_sent += 1
        else:
            still_failed += 1
    return now_sent, still_failed


# ── Sending ───────────────────────────────────────────────────────────────

def _deliver(key, kind, detail=''):
    """Claim `key`, then send. Returns the EmailLog row, or None (switched off / already done)."""
    if not email_enabled():
        return None
    try:
        with transaction.atomic():
            log = EmailLog.objects.create(key=key, kind=kind, detail=detail)
    except IntegrityError:
        return None                         # this exact email was already handled
    except Exception:
        logger.exception('Could not write the email log for %s', key)
        return None
    _attempt(log)
    return log


def _attempt(log):
    """Build and send the email for `log`, and record what happened. Never raises."""
    log.attempts += 1
    log.status, log.error = 'failed', ''
    try:
        obj, user = _load(log.key)
        log.recipient = (user.email or '').strip()
        if not log.recipient:
            log.status, log.error = 'skipped', 'The customer has no email address.'
        else:
            subject, context = _compose(log, obj, user)
            log.subject = subject[:200]
            message = EmailMultiAlternatives(subject, render_to_string('emails/message.txt', context),
                                             to=[log.recipient])
            message.attach_alternative(render_to_string('emails/message.html', context), 'text/html')
            if message.send(fail_silently=False) < 1:
                raise RuntimeError('No email was sent.')
            log.status = 'sent'
            log.sent_at = timezone.now()
            log.provider_id = getattr(message, 'brevo_message_id', '')[:200]
    except Exception as exc:
        log.status = 'failed'
        log.error = _hide_key(f'{type(exc).__name__}: {exc}')[:300]
        logger.error('Email %s failed: %s', log.key, log.error)
    try:
        log.save()
    except Exception:
        logger.exception('Could not save the email log for %s', log.key)


def _load(key):
    """Re-read the booking or payment named by an EmailLog key. Returns (object, customer)."""
    prefix, kind, pk = key.split(':')
    if kind == 'payment':
        obj = Payment.objects.select_related('user').get(pk=pk)
    else:
        obj = BOOKING_MODELS[kind].objects.select_related('user').get(pk=pk)
    return obj, obj.user


# ── Writing the email ─────────────────────────────────────────────────────

def _money(value):
    return f'₱{Decimal(value):,.2f}'


def _day(value):
    return format_date(value, 'F j, Y') if isinstance(value, date) else str(value)


def _clock(value):
    return format_date(value, 'g:i A') if isinstance(value, time) else ''


def _with_time(day, clock):
    text = _day(day)
    return f'{text} at {_clock(clock)}' if clock else text


def _summary(booking):
    """The 'what did I book' lines for any of the three booking types."""
    if isinstance(booking, HotelBooking):
        room = booking.room
        return [
            ('Hotel', room.hotel.name),
            ('Room', f'{room.room_number} ({room.get_room_type_display()})'),
            ('Check-in', _with_time(booking.check_in, booking.check_in_time)),
            ('Check-out', _with_time(booking.check_out, booking.check_out_time)),
        ]
    if isinstance(booking, EventBooking):
        event = booking.event
        return [
            ('Event', event.name),
            ('Date', _with_time(event.date, event.time)),
            ('Location', event.location),
            ('Tickets', str(booking.num_tickets)),
        ]
    service = booking.service
    return [
        ('Attraction', service.attraction.name),
        ('Service', service.name),
        ('Visit date', _day(booking.visit_date)),
        ('Guests', str(booking.num_guests)),
    ]


def _compose(log, obj, user):
    """Returns (subject, template context) for the email described by `log`."""
    from .views import CANCEL_WINDOW_HOURS       # imported here: views imports this module
    name = user.get_full_name() or user.username
    context = {
        'brand': 'Iligan Tourism',
        'site_url': settings.SITE_URL,
        'button_label': 'View my bookings',
        'button_url': settings.SITE_URL + reverse('my_bookings'),
        'footer': '',
    }
    greeting = f'Hi {name},'
    by_staff = log.detail == 'staff'

    if log.kind == KIND_UNFULFILLED:
        reference = f'PAY-{obj.pk}'
        context.update(
            heading='We could not complete your booking',
            paragraphs=[
                greeting,
                f'We received your payment, but we could not complete your booking: {log.detail}',
                f'Please contact us and quote payment reference {reference} so we can refund or rebook you.',
            ],
            details=[('Payment reference', reference), ('Amount paid', _money(obj.amount)),
                     ('Booking type', obj.get_booking_type_display())],
        )
        return f'Action needed: we could not complete your booking ({reference})', context

    reference = obj.reference_number
    details = _summary(obj) + [('Reference', reference), ('Total', _money(obj.total_price))]
    if log.kind == KIND_CONFIRMED:
        intro = ('Our team has confirmed your booking. Here are the details:' if by_staff else
                 'Thank you! Your payment was received and your booking is confirmed. Here are the details:')
        context.update(
            heading='Your booking is confirmed',
            paragraphs=[greeting, intro],
            details=details,
            footer=f'Need to cancel? You can cancel from My Bookings within {CANCEL_WINDOW_HOURS} hours of booking.',
        )
        return f'Booking confirmed: {reference}', context

    context.update(
        heading='Your booking was cancelled',
        paragraphs=[
            greeting,
            'Our team has cancelled your booking.' if by_staff else 'Your booking has been cancelled, as you requested.',
            'Refunds are reviewed and processed by our team. Please contact us and quote '
            f'reference {reference}.',
        ],
        details=details,
    )
    return f'Booking cancelled: {reference}', context
