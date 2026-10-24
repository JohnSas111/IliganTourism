"""
Tests for the My Bookings redesign: one unified list, tabs, type chips, search, one pager,
the single cancel rule shared by the list / details page / cancel action, and the details page.
No test touches the network (BaseCase blocks urlopen).
"""
from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import views
from .models import (
    Event, EventBooking, HotelBooking, Payment, ServiceBooking,
)
from .tests import BaseCase


def days(n):
    return date.today() + timedelta(days=n)


class BookingFactory:
    """Small helpers to create bookings of every type with unique references."""
    counter = 0

    def _ref(self, prefix):
        BookingFactory.counter += 1
        return f'{prefix}-T{BookingFactory.counter:06d}'

    def make_hotel(self, user=None, check_in=None, check_out=None, status='confirmed', age_hours=0, **kw):
        check_in = check_in if check_in is not None else days(5)
        check_out = check_out if check_out is not None else check_in + timedelta(days=1)
        b = HotelBooking.objects.create(
            user=user or self.customer, room=self.room, check_in=check_in, check_out=check_out,
            total_price=Decimal('2500.00'), status=status,
            reference_number=kw.pop('reference_number', self._ref('HTL')), **kw)
        return self._age(b, age_hours)

    def make_event(self, user=None, event=None, status='confirmed', age_hours=0, **kw):
        b = EventBooking.objects.create(
            user=user or self.customer, event=event or self.event, num_tickets=2,
            total_price=Decimal('100.00'), status=status,
            reference_number=kw.pop('reference_number', self._ref('EVT')), **kw)
        return self._age(b, age_hours)

    def make_service(self, user=None, visit_date=None, status='confirmed', age_hours=0, **kw):
        b = ServiceBooking.objects.create(
            user=user or self.customer, service=self.service,
            visit_date=visit_date if visit_date is not None else days(3), num_guests=1,
            total_price=Decimal('100.00'), status=status,
            reference_number=kw.pop('reference_number', self._ref('SVC')), **kw)
        return self._age(b, age_hours)

    @staticmethod
    def _age(booking, hours):
        """created_at is auto_now_add, so move it back with an update()."""
        if hours:
            type(booking).objects.filter(pk=booking.pk).update(
                created_at=timezone.now() - timedelta(hours=hours))
            booking.refresh_from_db()
        return booking


class ListTests(BookingFactory, BaseCase):
    def get(self, **params):
        return self.login(self.customer).get(reverse('my_bookings'), params)

    def test_login_is_required(self):
        response = self.client.get(reverse('my_bookings'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response['Location'])

    def test_one_list_shows_all_three_types_newest_first(self):
        old = self.make_hotel(age_hours=10, reference_number='HTL-OLDEST01')
        mid = self.make_event(age_hours=5, reference_number='EVT-MIDDLE01')
        new = self.make_service(age_hours=1, reference_number='SVC-NEWEST01')
        response = self.get()
        self.assertEqual(response.status_code, 200)
        refs = [r['reference'] for r in response.context['page_obj']]
        self.assertEqual(refs, [new.reference_number, mid.reference_number, old.reference_number])
        # The old three-section layout is gone.
        self.assertNotContains(response, 'Hotel Bookings')
        self.assertNotContains(response, 'Attraction Service Bookings')

    def test_only_own_bookings_are_listed(self):
        self.make_hotel(user=self.other_customer, reference_number='HTL-NOTMINE1')
        mine = self.make_hotel(reference_number='HTL-MINE0001')
        response = self.get()
        self.assertContains(response, mine.reference_number)
        self.assertNotContains(response, 'HTL-NOTMINE1')

    def test_tabs_split_upcoming_past_and_cancelled_with_counts(self):
        self.make_hotel(reference_number='HTL-UP000001')                                   # upcoming
        self.make_service(status='pending', reference_number='SVC-PEND0001')               # upcoming
        self.make_service(status='completed', visit_date=days(2), reference_number='SVC-DONE0001')   # past (completed)
        self.make_hotel(check_in=days(-5), check_out=days(-4), reference_number='HTL-OLD00001')      # past (date gone)
        self.make_event(status='cancelled', reference_number='EVT-CANC0001')                 # cancelled
        tabs = {t['key']: t['count'] for t in self.get().context['tabs']}
        self.assertEqual(tabs, {'upcoming': 2, 'past': 2, 'cancelled': 1})

        def refs(tab):
            return {r['reference'] for r in self.get(tab=tab).context['page_obj']}
        self.assertEqual(refs('upcoming'), {'HTL-UP000001', 'SVC-PEND0001'})
        self.assertEqual(refs('past'), {'SVC-DONE0001', 'HTL-OLD00001'})
        self.assertEqual(refs('cancelled'), {'EVT-CANC0001'})

    def test_a_stay_in_progress_is_still_upcoming_but_cannot_be_cancelled(self):
        self.make_hotel(check_in=days(-1), check_out=days(2), reference_number='HTL-NOW00001')
        response = self.get()
        rows = list(response.context['page_obj'])
        self.assertEqual([r['reference'] for r in rows], ['HTL-NOW00001'])
        self.assertFalse(rows[0]['can_cancel'])
        self.assertNotContains(response, 'Cancel booking')

    def test_type_chips_filter_and_tab_counts_follow(self):
        self.make_hotel(reference_number='HTL-CHIP0001')
        self.make_event(reference_number='EVT-CHIP0001')
        self.make_service(reference_number='SVC-CHIP0001')
        for key, prefix in (('hotels', 'HTL'), ('events', 'EVT'), ('attractions', 'SVC')):
            with self.subTest(chip=key):
                response = self.get(type=key)
                self.assertEqual([r['reference'][:3] for r in response.context['page_obj']], [prefix])
                self.assertEqual(response.context['tabs'][0]['count'], 1)
        self.assertEqual(len(self.get(type='all').context['page_obj']), 3)

    def test_search_matches_reference_or_name_ignoring_case(self):
        self.make_hotel(reference_number='HTL-FINDME01')
        self.make_event(reference_number='EVT-OTHER001')
        by_ref = self.get(q='findme')
        self.assertEqual([r['reference'] for r in by_ref.context['page_obj']], ['HTL-FINDME01'])
        by_name = self.get(q='test fiesta')
        self.assertEqual([r['reference'] for r in by_name.context['page_obj']], ['EVT-OTHER001'])
        by_hotel = self.get(q='TEST HOTEL')
        self.assertEqual([r['reference'] for r in by_hotel.context['page_obj']], ['HTL-FINDME01'])
        self.assertContains(self.get(q='zzz-nothing'), 'match')

    def test_search_text_is_escaped(self):
        self.make_hotel()
        response = self.get(q='<script>alert(1)</script>')
        self.assertNotContains(response, '<script>alert(1)</script>')
        self.assertContains(response, '&lt;script&gt;')

    def test_bad_parameters_fall_back_to_defaults(self):
        self.make_hotel()
        response = self.get(tab='nonsense', type='???', page='abc', q='x' * 500)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['tab'], 'upcoming')
        self.assertEqual(response.context['kind_key'], 'all')
        self.assertEqual(self.get(page='999').status_code, 200)

    def test_one_pager_ten_per_page_keeps_filters(self):
        for i in range(12):
            self.make_service(reference_number=f'SVC-PAGE{i:04d}')
        page1 = self.get(q='SVC-PAGE')
        self.assertEqual(len(page1.context['page_obj']), 10)
        self.assertEqual(page1.context['page_obj'].paginator.count, 12)
        self.assertIn('q=SVC-PAGE', page1.context['pager']['next'])
        self.assertIn('page=2', page1.context['pager']['next'])
        page2 = self.get(q='SVC-PAGE', page=2)
        self.assertEqual(len(page2.context['page_obj']), 2)
        self.assertContains(page2, 'Showing 11–12 of 12 bookings')

    def test_tab_and_chip_links_keep_each_other_and_the_search(self):
        response = self.get(tab='past', type='hotels', q='abc')
        tabs = {t['key']: t['url'] for t in response.context['tabs']}
        self.assertIn('type=hotels', tabs['cancelled'])
        self.assertIn('q=abc', tabs['cancelled'])
        self.assertIn('tab=cancelled', tabs['cancelled'])
        kinds = {k['key']: k['url'] for k in response.context['kinds']}
        self.assertIn('tab=past', kinds['events'])
        self.assertIn('q=abc', kinds['events'])

    def test_empty_states(self):
        self.assertContains(self.get(), 'You have no bookings yet')
        self.make_hotel(status='cancelled')
        empty_tab = self.get()      # has a booking, but none upcoming
        self.assertContains(empty_tab, 'No upcoming bookings here')
        self.assertNotContains(empty_tab, 'You have no bookings yet')

    def test_cards_show_name_reference_price_status_and_one_date_format(self):
        self.make_hotel(check_in=date(2030, 3, 4), check_out=date(2030, 3, 6), reference_number='HTL-FORMAT01')
        html = self.get().content.decode()
        self.assertIn('HTL-FORMAT01', html)
        self.assertIn('Mar 4, 2030 &ndash; Mar 6, 2030', html)
        self.assertIn('2 nights', html)
        self.assertIn('&#8369;2,500.00', html)
        self.assertIn('Confirmed', html)
        self.assertNotIn('March 4, 2030', html)      # the old long format

    def test_cancelled_is_shown_once_per_card(self):
        self.make_hotel(status='cancelled')
        html = self.get(tab='cancelled').content.decode()
        self.assertEqual(html.count('bk-pill bk-pill--cancelled'), 1)


class NextUpTests(BookingFactory, BaseCase):
    def banner(self):
        return self.login(self.customer).get(reverse('my_bookings')).context['next_up']

    def test_none_without_upcoming_bookings(self):
        self.make_hotel(status='cancelled')
        self.make_hotel(check_in=days(-9), check_out=days(-8))
        self.assertIsNone(self.banner())

    def test_picks_the_soonest_start_and_words_it(self):
        self.make_hotel(check_in=days(9), reference_number='HTL-LATER001')
        soon = self.make_service(visit_date=days(1), reference_number='SVC-SOON0001')
        self.make_service(visit_date=days(-1), reference_number='SVC-GONE0001')              # past: ignored
        self.make_service(visit_date=days(0), status='cancelled', reference_number='SVC-CANC0001')  # cancelled: ignored
        banner = self.banner()
        self.assertEqual(banner['reference'], soon.reference_number)
        self.assertEqual(banner['when_text'], 'tomorrow')

    def test_banner_is_rendered_with_wording_per_type(self):
        self.make_hotel(check_in=days(3), reference_number='HTL-BANNER01')
        response = self.login(self.customer).get(reverse('my_bookings'))
        self.assertContains(response, 'Next up:')
        self.assertContains(response, 'check-in')
        self.assertContains(response, 'in 3 days')

    def test_today(self):
        self.make_service(visit_date=days(0))
        self.assertEqual(self.banner()['when_text'], 'today')


class CancelRuleConsistencyTests(BookingFactory, BaseCase):
    """The list, the details page and the cancel action must always agree."""

    def _matrix(self):
        return {
            'ok_confirmed':   self.make_hotel(reference_number='HTL-C-OK0001'),
            'ok_pending':     self.make_service(status='pending', reference_number='SVC-C-PEND01'),
            'completed':      self.make_service(status='completed', reference_number='SVC-C-DONE01'),
            'cancelled':      self.make_event(status='cancelled', reference_number='EVT-C-CANC01'),
            'date_passed':    self.make_hotel(check_in=days(-2), check_out=days(-1), reference_number='HTL-C-PAST01'),
            'window_closed':  self.make_service(age_hours=30, reference_number='SVC-C-LATE01'),
        }

    def test_one_rule_decides_button_on_list_and_on_details_and_the_action(self):
        client = self.login(self.customer)
        expectations = {'ok_confirmed': True, 'ok_pending': True, 'completed': False,
                        'cancelled': False, 'date_passed': False, 'window_closed': False}
        bookings = self._matrix()
        list_html = client.get(reverse('my_bookings'), {'tab': 'upcoming'}).content.decode()
        for name, booking in bookings.items():
            kind = views._booking_kind(booking)
            expected = expectations[name]
            with self.subTest(case=name):
                self.assertEqual(views._cancel_block_reason(booking) is None, expected)
                cancel_url = reverse(views.CANCEL_URL_NAMES[kind], args=[booking.pk])
                # details page
                detail = client.get(reverse('booking_detail', args=[kind, booking.pk]))
                self.assertEqual(f'data-cancel-url="{cancel_url}"' in detail.content.decode(), expected)
        # list page (every tab, since some cases are past/cancelled)
        for tab in ('upcoming', 'past', 'cancelled'):
            list_html = client.get(reverse('my_bookings'), {'tab': tab}).content.decode()
            for name, booking in bookings.items():
                kind = views._booking_kind(booking)
                cancel_url = reverse(views.CANCEL_URL_NAMES[kind], args=[booking.pk])
                if f'data-cancel-url="{cancel_url}"' in list_html:
                    self.assertTrue(expectations[name], f'{name} shows Cancel on the list but is not cancellable')
        # the action refuses exactly the ones the pages hide
        for name, booking in bookings.items():
            if not expectations[name]:
                kind = views._booking_kind(booking)
                before = booking.status
                client.post(reverse(views.CANCEL_URL_NAMES[kind], args=[booking.pk]))
                booking.refresh_from_db()
                self.assertEqual(booking.status, before, name)

    def test_list_offers_cancel_for_a_cancellable_booking_and_the_action_then_works(self):
        booking = self.make_hotel(reference_number='HTL-WORKS001')
        client = self.login(self.customer)
        html = client.get(reverse('my_bookings')).content.decode()
        self.assertIn(reverse('cancel_hotel_booking', args=[booking.pk]), html)
        self.assertIn('left to cancel', html)
        client.post(reverse('cancel_hotel_booking', args=[booking.pk]))
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'cancelled')

    def test_completed_booking_has_no_cancel_button_on_the_list(self):
        self.make_service(status='completed')
        response = self.login(self.customer).get(reverse('my_bookings'), {'tab': 'past'})
        self.assertNotContains(response, 'Cancel booking')

    def test_cancel_flash_messages_are_unchanged(self):
        client = self.login(self.customer)
        late = self.make_service(age_hours=30)
        response = client.post(reverse('cancel_service_booking', args=[late.pk]))
        self.assertIn('Cancellation window has passed', ' '.join(self.messages(response)))
        gone = self.make_hotel(check_in=days(-2), check_out=days(-1))
        response = client.post(reverse('cancel_hotel_booking', args=[gone.pk]))
        self.assertIn('already passed', ' '.join(self.messages(response)))

    def test_time_left_text(self):
        f = views._format_time_left
        self.assertEqual(f(timedelta(hours=18, minutes=20, seconds=30)), '18h 20m')
        self.assertEqual(f(timedelta(minutes=45)), '45m')
        self.assertEqual(f(timedelta(hours=1)), '1h 0m')
        self.assertEqual(f(timedelta(seconds=-50)), '0m')

    def test_soon_flag_under_two_hours(self):
        late = self.make_service(age_hours=23)
        row = views._booking_row(late)
        self.assertTrue(row['can_cancel'])
        self.assertTrue(row['cancel_soon'])
        fresh = views._booking_row(self.make_service())
        self.assertFalse(fresh['cancel_soon'])


class DetailTests(BookingFactory, BaseCase):
    def detail(self, kind, booking):
        return self.login(self.customer).get(reverse('booking_detail', args=[kind, booking.pk]))

    def test_all_three_types_show_header_facts_payment_status_cancel_and_help(self):
        cases = (
            ('hotel', self.make_hotel(reference_number='HTL-DETAIL01'), ['Your stay', 'Check-in', 'Max guests', 'Room 101']),
            ('event', self.make_event(reference_number='EVT-DETAIL01'), ['Your event', 'Tickets', 'Plaza']),
            ('service', self.make_service(reference_number='SVC-DETAIL01', notes='Near the window'),
             ['Your visit', 'Visit date', 'Near the window', 'Day Pass']),
        )
        for kind, booking, needles in cases:
            with self.subTest(kind=kind):
                response = self.detail(kind, booking)
                self.assertEqual(response.status_code, 200)
                html = response.content.decode()
                for needle in [booking.reference_number, 'Payment', 'Status', 'Need to cancel?',
                               'Need help?', 'Contact us', 'Back to My Bookings',
                               reverse('cancel_' + kind + '_booking', args=[booking.pk])] + needles:
                    self.assertIn(needle, html)

    def test_pending_booking_can_be_cancelled_from_the_details_page(self):
        booking = self.make_service(status='pending')
        html = self.detail('service', booking).content.decode()
        self.assertIn(reverse('cancel_service_booking', args=[booking.pk]), html)

    def test_confirmed_booking_can_be_cancelled_from_the_details_page(self):
        booking = self.make_hotel()
        html = self.detail('hotel', booking).content.decode()
        self.assertIn(reverse('cancel_hotel_booking', args=[booking.pk]), html)
        self.assertIn('Refunds are reviewed', html)

    def test_explains_why_cancelling_is_not_possible(self):
        cases = (
            (self.make_service(status='completed'), 'completed'),
            (self.make_hotel(status='cancelled'), 'has been cancelled'),
            (self.make_service(age_hours=30), 'Cancellation window has passed'),
            (self.make_hotel(check_in=days(-2), check_out=days(-1)), 'already passed'),
        )
        for booking, text in cases:
            with self.subTest(text=text):
                kind = views._booking_kind(booking)
                html = self.detail(kind, booking).content.decode()
                self.assertIn(text, html)
                self.assertNotIn('data-cancel-url="', html)

    def test_payment_section_uses_the_linked_payment(self):
        booking = self.make_hotel()
        payment = Payment.objects.create(
            user=self.customer, booking_type='hotel', booking_data={}, amount=Decimal('2500.00'),
            status='paid', hotel_booking=booking)
        html = self.detail('hotel', booking).content.decode()
        self.assertIn('Total paid', html)
        self.assertIn(f'PAY-{payment.pk}', html)
        self.assertIn('Paid online', html)

    def test_payment_section_without_a_payment_record(self):
        html = self.detail('hotel', self.make_hotel()).content.decode()
        self.assertIn('No online payment record', html)
        self.assertNotIn('Total paid', html)

    def test_status_steps(self):
        self.assertEqual(views.booking_detail.__name__, 'booking_detail')
        cancelled = self.detail('hotel', self.make_hotel(status='cancelled')).context['steps']
        self.assertEqual([s[0] for s in cancelled], ['Booked', 'Cancelled'])
        done = self.detail('service', self.make_service(status='completed')).context['steps']
        self.assertEqual([s[0] for s in done], ['Confirmed', 'Completed'])
        live = self.detail('event', self.make_event()).context['steps']
        self.assertEqual(live[1][0], 'Completed after the event')

    def test_unknown_type_goes_back_to_the_list_and_strangers_get_404(self):
        booking = self.make_hotel()
        client = self.login(self.customer)
        self.assertRedirects(client.get(reverse('booking_detail', args=['spaceship', 1])),
                             reverse('my_bookings'), fetch_redirect_response=False)
        stranger = self.login(self.other_customer)
        with self.assertLogs('django.request', level='WARNING'):
            self.assertEqual(stranger.get(reverse('booking_detail', args=['hotel', booking.pk])).status_code, 404)

    def test_contact_link_and_modal_are_present(self):
        html = self.detail('hotel', self.make_hotel()).content.decode()
        self.assertIn(reverse('contact'), html)
        self.assertIn('id="cancelModal"', html)
        self.assertIn('csrfmiddlewaretoken', html)
