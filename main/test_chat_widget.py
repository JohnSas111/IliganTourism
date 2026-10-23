"""
Tests for the chat assistant widget fixes (question buttons hiding, chat kept per tab).

The widget's behaviour is JavaScript, which Django tests cannot run. These tests guard the
parts Django renders: the markup, the CSS order and the login flag. The behaviour itself was
checked in a simulated browser (jsdom) with 17 scenarios before delivery.

Run only these:   py manage.py test main.test_chat_widget
"""
from django.test import override_settings
from django.urls import reverse

from .tests import BaseCase


@override_settings(GEMINI_API_KEY='test-key-not-real')
class ChatWidgetPageTests(BaseCase):

    def widget(self, client=None):
        page = (client or self.client).get(reverse('home'))
        self.assertEqual(page.status_code, 200)
        return page.content.decode()

    def test_the_widget_tells_the_script_whether_the_visitor_is_logged_in(self):
        self.assertIn('data-auth="0"', self.widget())
        self.assertIn('data-auth="1"', self.widget(self.login(self.customer)))

    def test_the_new_chat_button_is_there_and_starts_hidden(self):
        html = self.widget()
        self.assertRegex(html, r'<button[^>]*id="aiNew"[^>]*\bhidden\b')

    def test_the_hidden_rule_for_the_question_buttons_comes_after_the_display_rule(self):
        """If it came first, hiding the buttons would silently do nothing (the original bug)."""
        html = self.widget()
        shows, hides = html.index('.ai-chips {'), html.index('.ai-chips[hidden]')
        self.assertLess(shows, hides)

    def test_the_chat_is_kept_for_the_tab_only_never_permanently(self):
        html = self.widget()
        self.assertIn('sessionStorage', html)
        self.assertNotIn('localStorage', html)

    def test_no_widget_when_the_assistant_is_switched_off(self):
        with override_settings(GEMINI_API_KEY=''):
            self.assertNotIn('id="aiAssistant"', self.widget())
