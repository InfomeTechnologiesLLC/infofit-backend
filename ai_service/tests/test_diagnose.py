import io
import json
import urllib.error
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import Role, User
from ai_service import provider
from notifications.models import SchedulerHeartbeat

PW = "Strong-passphrase-123"


def http_error(code, message):
    body = io.BytesIO(json.dumps({"error": {"code": code, "message": message}}).encode())
    return urllib.error.HTTPError("https://x", code, "err", {}, body)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@override_settings(GEMINI_API_KEY="k-test", GEMINI_MODEL="gemini-2.5-flash-lite")
class DiagnoseTests(TestCase):
    def test_rejected_key_is_explained_and_recorded(self):
        with patch("ai_service.provider.urllib.request.urlopen",
                   side_effect=http_error(400, "API key not valid. Please pass a valid API key.")):
            ok, text = provider.diagnose()
        self.assertFalse(ok)
        self.assertIn("Gemini answered 400", text)
        self.assertIn("API key not valid", text)
        self.assertNotIn("k-test", text)
        beat = SchedulerHeartbeat.objects.get(name="iva")
        self.assertFalse(beat.last_ok)

    def test_quota_and_missing_model(self):
        self.assertIn("quota", provider.failure_reason(http_error(429, "Resource exhausted")))
        self.assertIn("model name is not available", provider.failure_reason(http_error(404, "not found")))

    def test_success(self):
        payload = {"candidates": [{"content": {"parts": [{"text": "OK"}]}}]}
        with patch("ai_service.provider.urllib.request.urlopen",
                   return_value=FakeResponse(json.dumps(payload).encode())):
            self.assertEqual(provider.diagnose(), (True, "Gemini (gemini-2.5-flash-lite) answered: OK"))
        self.assertTrue(SchedulerHeartbeat.objects.get(name="iva").last_ok)

    def test_chat_failure_shows_on_portal_with_test_button(self):
        staff = User.objects.create_user("s@example.test", PW, role=Role.ADMIN, is_staff=True)
        with patch("ai_service.provider.urllib.request.urlopen", side_effect=http_error(403, "Permission denied")):
            with self.assertRaises(provider.AiUnavailable):
                provider.complete([{"role": "user", "content": "hi"}], "gemini-2.5-flash-lite")
        self.client.force_login(staff)
        page = self.client.get(reverse("portal:notifications"))
        self.assertContains(page, "Gemini answered 403")
        self.assertContains(page, "Test IVA")
        with patch("ai_service.provider.urllib.request.urlopen", side_effect=urllib.error.URLError("timed out")):
            response = self.client.post(reverse("portal:notifications"), {"action": "ai_test"}, follow=True)
        self.assertContains(response, "IVA test: The server cannot reach Gemini")


class NotConfiguredTests(TestCase):
    @override_settings(GEMINI_API_KEY="")
    def test_missing_key(self):
        ok, text = provider.diagnose()
        self.assertFalse(ok)
        self.assertIn("not configured", text)
