from io import BytesIO
from unittest.mock import patch
import base64
import json
import urllib.error

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from PIL import Image
from rest_framework.test import APITestCase

from accounts.models import User
from fitness.models import DailyActivity, FitnessAccount, FitnessSource
from ai_service.provider import AiUnavailable, complete
from ai_service.nutrition import _energy_kcal, search_foods, NutritionUnavailable
from wellness.models import HydrationLog


class AiApiTests(APITestCase):
    def setUp(self):
        self.alice = User.objects.create_user(email="alice@example.test", password="Strong-Passphrase-123")
        self.bob = User.objects.create_user(email="bob@example.test", password="Strong-Passphrase-123")
        self.client.force_authenticate(self.alice)

    @override_settings(GEMINI_API_KEY="", GEMINI_MODEL="gemini-test")
    def test_missing_provider_configuration_is_explicit(self):
        response = self.client.post("/api/v1/ai/chat/", {"message": "Help me sleep"}, format="json")
        self.assertEqual(response.status_code, 503)
        self.assertIn("not configured", str(response.data))

    @patch("ai_service.views.complete", return_value="A short general answer")
    def test_chat_sends_current_message_once_and_no_personal_data_for_general_question(self, complete):
        response = self.client.post("/api/v1/ai/chat/", {
            "message": "How can I stretch?", "history": [{"role": "user", "content": "Hello"}]
        }, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["used_context"], [])
        messages = complete.call_args.args[0]
        self.assertEqual([row["content"] for row in messages if row["role"] == "user"],
                         ["Hello", "How can I stretch?"])
        self.assertNotIn("alice@example.test", str(messages))

    @patch("ai_service.views.complete", return_value="Your latest recorded steps are 123.")
    def test_read_only_context_is_scoped_to_signed_in_employee(self, complete):
        from uuid import uuid4
        from django.utils import timezone

        for user, steps in ((self.alice, 123), (self.bob, 9876)):
            source = FitnessSource.objects.create(user=user, installation_id=uuid4())
            FitnessAccount.objects.create(user=user, active_source=source, time_zone="UTC")
            DailyActivity.objects.create(
                user=user, source=source, local_date=timezone.now().date(), time_zone="UTC",
                steps=steps, metric_status={"steps": "available"}, observed_at=timezone.now(),
            )
        response = self.client.post("/api/v1/ai/chat/", {"message": "How many are my steps?"}, format="json")
        self.assertEqual(response.status_code, 200)
        # The saved goal is included with any fitness question; today's steps only for personal ones.
        self.assertEqual(response.data["used_context"], ["goal", "fitness"])
        supplied = str(complete.call_args.args[0])
        self.assertIn("123", supplied)
        self.assertNotIn("9876", supplied)
        self.assertEqual(DailyActivity.objects.get(user=self.alice).steps, 123)

    @patch("ai_service.views.complete", return_value="You logged 250 ml today.")
    def test_hydration_context_is_self_reported_and_employee_scoped(self, complete):
        from uuid import uuid4
        from django.utils import timezone

        HydrationLog.objects.create(user=self.alice, client_request_id=uuid4(), volume_ml=250,
                                    local_date=timezone.now().date(), time_zone="UTC")
        HydrationLog.objects.create(user=self.bob, client_request_id=uuid4(), volume_ml=900,
                                    local_date=timezone.now().date(), time_zone="UTC")
        response = self.client.post("/api/v1/ai/chat/", {"message": "How much water did I log today?"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["used_context"], ["hydration"])
        supplied = str(complete.call_args.args[0])
        self.assertIn("250", supplied)
        self.assertNotIn("900", supplied)

    @patch("ai_service.views.complete", return_value="I can see rice and vegetables; the sauce is unclear.")
    def test_lens_sends_photo_and_no_invented_nutrition(self, complete):
        buffer = BytesIO()
        exif = Image.Exif()
        exif[270] = "private image metadata"
        Image.new("RGB", (24, 24), color="green").save(buffer, format="JPEG", exif=exif)
        photo = SimpleUploadedFile("meal.jpg", buffer.getvalue(), content_type="image/jpeg")
        response = self.client.post("/api/v1/ai/lens/", {"image": photo}, format="multipart")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["nutrition_available"])
        self.assertNotIn("kcal", response.data)
        self.assertIn("data:image/jpeg;base64,", str(complete.call_args.args[0]))
        data_url = complete.call_args.args[0][1]["content"][1]["image_url"]["url"]
        cleaned = Image.open(BytesIO(base64.b64decode(data_url.split(",", 1)[1])))
        self.assertFalse(cleaned.getexif())

    def test_invalid_inputs_and_anonymous_access(self):
        self.assertEqual(self.client.post("/api/v1/ai/chat/", {"message": " "}, format="json").status_code, 400)
        self.assertEqual(self.client.post("/api/v1/ai/lens/", {
            "image": SimpleUploadedFile("fake.jpg", b"not an image", content_type="image/jpeg")
        }, format="multipart").status_code, 400)
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.post("/api/v1/ai/chat/", {"message": "Hi"}, format="json").status_code, 401)

    @patch("ai_service.views.search_foods", return_value=[{
        "fdc_id": 123, "description": "Doughnut", "data_type": "SR Legacy", "kcal_per_100g": 452.0,
    }])
    def test_authenticated_food_search_and_validation(self, lookup):
        response = self.client.get("/api/v1/ai/foods/", {"q": "doughnut"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["results"][0]["kcal_per_100g"], 452.0)
        lookup.assert_called_once_with("doughnut")
        self.assertEqual(self.client.get("/api/v1/ai/foods/", {"q": "x"}).status_code, 400)
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get("/api/v1/ai/foods/", {"q": "doughnut"}).status_code, 401)


class NutritionTests(APITestCase):
    @override_settings(USDA_FDC_API_KEY="test_key")
    @patch("ai_service.nutrition.urllib.request.urlopen")
    def test_search_uses_usda_energy_per_100g_and_omits_missing_values(self, urlopen):
        urlopen.return_value = BytesIO(json.dumps({"foods": [
            {"fdcId": 1, "description": "Doughnut", "dataType": "SR Legacy",
             "foodNutrients": [{"nutrientId": 1008, "unitName": "KCAL", "value": 452}]},
            {"fdcId": 2, "description": "Unknown", "foodNutrients": []},
            {"fdcId": 3, "description": "Foundation food", "dataType": "Foundation",
             "foodNutrients": [{"nutrientId": 2047, "unitName": "kcal", "value": 220}]},
        ]}).encode())
        self.assertEqual(search_foods("doughnut"), [
            {"fdc_id": 1, "description": "Doughnut", "data_type": "SR Legacy", "kcal_per_100g": 452.0},
            {"fdc_id": 3, "description": "Foundation food", "data_type": "Foundation", "kcal_per_100g": 220.0},
        ])
        request = urlopen.call_args.args[0]
        self.assertIn("api.nal.usda.gov/fdc/v1/foods/search", request.full_url)
        self.assertEqual(json.loads(request.data)["query"], "doughnut")

    @override_settings(USDA_FDC_API_KEY="")
    def test_unconfigured_search_is_unavailable(self):
        with self.assertRaises(NutritionUnavailable):
            search_foods("doughnut")

    def test_invalid_energy_is_not_presented_as_zero(self):
        self.assertIsNone(_energy_kcal({"foodNutrients": []}))
        self.assertIsNone(_energy_kcal({"foodNutrients": [{"nutrientId": 1008, "unitName": "KCAL", "value": -1}]}))


class ProviderTests(APITestCase):
    @override_settings(GEMINI_API_KEY="gemini_test_only")
    @patch("ai_service.provider.urllib.request.urlopen")
    def test_server_sends_key_in_header_to_fixed_endpoint_and_reads_answer(self, urlopen):
        urlopen.return_value = BytesIO(json.dumps({
            "candidates": [{"content": {"parts": [{"text": "A real provider response"}]}}]
        }).encode())
        self.assertEqual(complete([{"role": "system", "content": "Be brief"}, {"role": "user", "content": "Hello"}],
                                  "gemini-test"), "A real provider response")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url,
                         "https://generativelanguage.googleapis.com/v1beta/models/gemini-test:generateContent")
        self.assertEqual(request.get_header("X-goog-api-key"), "gemini_test_only")
        self.assertNotIn("gemini_test_only", request.full_url)
        body = json.loads(request.data)
        self.assertEqual(body["systemInstruction"]["parts"][0]["text"], "Be brief")
        self.assertEqual(body["contents"], [{"role": "user", "parts": [{"text": "Hello"}]}])

    @override_settings(GEMINI_API_KEY="gemini_test_only")
    def test_model_name_cannot_change_the_url(self):
        with self.assertRaises(AiUnavailable):
            complete([{"role": "user", "content": "Hello"}], "../other?x=1")

    @override_settings(GEMINI_API_KEY="gemini_test_only")
    @patch("ai_service.provider.urllib.request.urlopen")
    def test_stream_yields_text_and_function_calls(self, urlopen):
        from ai_service.provider import stream
        chunks = [
            {"candidates": [{"content": {"parts": [{"text": "Hel"}]}}]},
            {"candidates": [{"content": {"parts": [{"text": "lo"}]}}]},
            {"candidates": [{"content": {"parts": [{"functionCall": {"name": "get_today_summary", "args": {}}}]}}]},
        ]
        urlopen.return_value = BytesIO("".join(f"data: {json.dumps(c)}\r\n\r\n" for c in chunks).encode())
        parts = list(stream("", [{"role": "user", "parts": [{"text": "hi"}]}], "gemini-test",
                            tools=[{"name": "get_today_summary", "description": "x"}]))
        self.assertEqual([p.get("text") for p in parts[:2]], ["Hel", "lo"])
        self.assertEqual(parts[2]["functionCall"]["name"], "get_today_summary")
        request = urlopen.call_args.args[0]
        self.assertTrue(request.full_url.endswith(":streamGenerateContent?alt=sse"))
        self.assertIn("functionDeclarations", json.loads(request.data)["tools"][0])

    @override_settings(GEMINI_API_KEY="gemini_test_only")
    @patch("ai_service.provider.urllib.request.urlopen", side_effect=urllib.error.URLError("private provider detail"))
    def test_provider_error_does_not_reveal_detail(self, _urlopen):
        with self.assertRaises(AiUnavailable) as raised:
            complete([{"role": "user", "content": "Hello"}], "gemini-test")
        self.assertNotIn("private provider detail", str(raised.exception))
