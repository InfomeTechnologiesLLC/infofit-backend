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

    @override_settings(HUGGINGFACE_API_KEY="", HUGGINGFACE_MODEL="model")
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
        self.assertEqual(response.data["used_context"], ["fitness"])
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
    @override_settings(HUGGINGFACE_API_KEY="hf_test_only")
    @patch("ai_service.provider.urllib.request.urlopen")
    def test_server_sends_token_to_fixed_router_and_reads_answer(self, urlopen):
        urlopen.return_value = BytesIO(json.dumps({
            "choices": [{"message": {"content": "A real provider response"}}]
        }).encode())
        self.assertEqual(complete([{"role": "user", "content": "Hello"}], "test/model"), "A real provider response")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://router.huggingface.co/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer hf_test_only")
        self.assertEqual(json.loads(request.data)["model"], "test/model")

    @override_settings(HUGGINGFACE_API_KEY="hf_test_only")
    @patch("ai_service.provider.urllib.request.urlopen", side_effect=urllib.error.URLError("private provider detail"))
    def test_provider_error_does_not_reveal_detail(self, _urlopen):
        with self.assertRaises(AiUnavailable) as raised:
            complete([{"role": "user", "content": "Hello"}], "test/model")
        self.assertNotIn("private provider detail", str(raised.exception))
