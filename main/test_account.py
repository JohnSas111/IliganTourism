"""
Tests for the Account Settings page (/account/): profile (names + email) and password change.
"""
from django.contrib.auth.models import User
from django.core import mail
from django.test import override_settings
from django.urls import reverse

from . import notifications
from .test_my_bookings import BookingFactory
from .tests import PASSWORD, BaseCase

NEW_PASSWORD = 'Brand-new-pass-98765'


class AccountBase(BookingFactory, BaseCase):
    def setUp(self):
        super().setUp()
        self.url = reverse('account_settings')
        self.client = self.login(self.customer)

    def save_profile(self, **values):
        data = {'action': 'profile', 'first_name': 'Ana', 'last_name': 'Cruz', 'email': 'ana@example.com'}
        data.update(values)
        return self.client.post(self.url, data)

    def change_password(self, **values):
        data = {'action': 'password', 'old_password': PASSWORD,
                'new_password1': NEW_PASSWORD, 'new_password2': NEW_PASSWORD}
        data.update(values)
        return self.client.post(self.url, data)

    def reload(self):
        self.customer.refresh_from_db()
        return self.customer


class AccessAndPageTests(AccountBase):
    def test_login_is_required(self):
        anonymous = self.__class__.client_class()
        for response in (anonymous.get(self.url), anonymous.post(self.url, {'action': 'profile'})):
            self.assertEqual(response.status_code, 302)
            self.assertIn('login', response['Location'])

    def test_page_shows_both_forms_prefilled_and_a_read_only_username(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn('Profile', html)
        self.assertIn('Change password', html)
        self.assertIn('value="c@example.com"', html)
        self.assertIn('Current password', html)
        self.assertRegex(html, r'id="acUsername"[^>]*value="customer"[^>]*readonly|value="customer"[^>]*readonly')
        self.assertIn('csrfmiddlewaretoken', html)

    def test_menu_link_for_logged_in_users_only(self):
        self.assertContains(self.client.get(reverse('home')), reverse('account_settings'))
        self.assertNotContains(self.__class__.client_class().get(reverse('home')), reverse('account_settings'))

    def test_names_are_shown_escaped(self):
        self.customer.first_name = '<script>alert(1)</script>'
        self.customer.save()
        html = self.client.get(self.url).content.decode()
        self.assertNotIn('<script>alert(1)</script>', html)
        self.assertIn('&lt;script&gt;', html)

    def test_unknown_action_changes_nothing(self):
        response = self.client.post(self.url, {'action': 'hack', 'email': 'x@example.com'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.reload().email, 'c@example.com')


class ProfileTests(AccountBase):
    def test_saves_names_and_email_then_redirects_with_a_message(self):
        response = self.save_profile()
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        user = self.reload()
        self.assertEqual((user.first_name, user.last_name, user.email), ('Ana', 'Cruz', 'ana@example.com'))
        self.assertIn('Your details have been saved.', self.messages(response))

    def test_email_is_required_and_must_look_like_an_email(self):
        for bad in ('', 'not-an-email'):
            with self.subTest(email=bad):
                response = self.save_profile(email=bad)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context['profile_form'].errors['email'])
                self.assertEqual(self.reload().email, 'c@example.com')
        self.assertContains(self.save_profile(email=''), 'Please fix the problems below.')

    def test_email_used_by_another_account_is_refused_ignoring_case(self):
        response = self.save_profile(email='O@Example.com')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'already used by another account')
        self.assertEqual(self.reload().email, 'c@example.com')

    def test_keeping_your_own_email_is_fine(self):
        response = self.save_profile(email='C@EXAMPLE.COM')
        self.assertEqual(response.status_code, 302)

    def test_a_shared_old_email_does_not_block_saving_other_details(self):
        User.objects.filter(pk=self.other_customer.pk).update(email='c@example.com')   # legacy duplicate
        response = self.save_profile(email='c@example.com', first_name='Newname')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.reload().first_name, 'Newname')

    def test_names_are_optional_so_staff_accounts_without_names_can_save(self):
        client = self.login(self.admin)
        response = client.post(self.url, {'action': 'profile', 'first_name': '', 'last_name': '',
                                          'email': 'boss@example.com'})
        self.assertEqual(response.status_code, 302)
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.email, 'boss@example.com')

    def test_an_account_without_an_email_can_add_one(self):
        client = self.login(self.hotel_staff)          # created without an email
        client.post(self.url, {'action': 'profile', 'first_name': '', 'last_name': '', 'email': 'desk@example.com'})
        self.hotel_staff.refresh_from_db()
        self.assertEqual(self.hotel_staff.email, 'desk@example.com')

    def test_other_fields_in_the_request_are_ignored(self):
        self.save_profile(username='hacker', is_staff='on', is_superuser='on', password='x', is_active='')
        user = self.reload()
        self.assertEqual(user.username, 'customer')
        self.assertFalse(user.is_staff or user.is_superuser)
        self.assertTrue(user.is_active)
        self.assertTrue(user.check_password(PASSWORD))

    def test_a_rejected_form_does_not_change_anything_in_the_database(self):
        self.save_profile(first_name='Changed', email='o@example.com')
        user = self.reload()
        self.assertEqual((user.first_name, user.email), ('', 'c@example.com'))

    def test_users_can_only_change_their_own_account(self):
        self.save_profile(first_name='Mine')
        self.other_customer.refresh_from_db()
        self.assertEqual(self.other_customer.first_name, '')

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
    def test_booking_emails_go_to_the_new_address(self):
        booking = self.make_service(reference_number='SVC-ACCOUNT01')
        self.save_profile(email='newaddress@example.com')
        notifications.booking_confirmed(booking)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['newaddress@example.com'])


class PasswordTests(AccountBase):
    def test_changing_the_password_works_and_keeps_you_logged_in(self):
        response = self.change_password()
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.assertIn('Your password has been changed.', self.messages(response))
        user = self.reload()
        self.assertTrue(user.check_password(NEW_PASSWORD))
        self.assertFalse(user.check_password(PASSWORD))
        self.assertEqual(self.client.get(reverse('my_bookings')).status_code, 200)   # still logged in
        self.assertTrue(self.__class__.client_class().login(username='customer', password=NEW_PASSWORD))

    def test_wrong_current_password_is_refused(self):
        response = self.change_password(old_password='not-my-password')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['password_form'].errors['old_password'])
        self.assertContains(response, 'Please fix the problems below.')
        self.assertTrue(self.reload().check_password(PASSWORD))

    def test_new_passwords_must_match(self):
        response = self.change_password(new_password2='Something-else-12345')
        self.assertTrue(response.context['password_form'].errors['new_password2'])
        self.assertTrue(self.reload().check_password(PASSWORD))

    def test_weak_passwords_are_refused_by_the_site_password_rules(self):
        for weak in ('short1A', '1234567890123', 'password', 'customer1234'):
            with self.subTest(password=weak):
                response = self.change_password(new_password1=weak, new_password2=weak)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context['password_form'].errors['new_password2'])
                self.assertTrue(self.reload().check_password(PASSWORD))

    def test_the_password_rules_are_listed_on_the_page(self):
        self.assertContains(self.client.get(self.url), 'at least 8 characters')

    def test_a_password_error_leaves_the_profile_form_untouched(self):
        response = self.change_password(old_password='wrong')
        self.assertEqual(response.context['profile_form'].initial['email'], 'c@example.com')

    def test_other_logins_are_signed_out_but_this_one_is_not(self):
        second = self.login(self.customer)
        self.change_password()
        self.assertEqual(second.get(reverse('my_bookings')).status_code, 302)
        self.assertEqual(self.client.get(reverse('my_bookings')).status_code, 200)
