from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from assistant.tools import get_bmi
from wellness import bmi_advice


class BmiGuidanceTests(TestCase):
    def test_categories_and_healthy_range(self):
        self.assertEqual(bmi_advice.category(18.4), "underweight")
        self.assertEqual(bmi_advice.category(24.9), "healthy_range")
        self.assertEqual(bmi_advice.category(25.1), "overweight")
        self.assertEqual(bmi_advice.category(30), "obesity")
        self.assertEqual(bmi_advice.healthy_weight_range(170), (53.5, 72.0))

    def test_api_and_iva_tool(self):
        user = User.objects.create_user("b@x.test", "Strong-passphrase-123")
        api = APIClient()
        api.force_authenticate(user)
        self.assertIsNone(api.get("/api/v1/wellness/bmi/").json()["guidance"])
        self.assertFalse(get_bmi(user, {})["available"])
        data = api.post("/api/v1/wellness/bmi/", {"height_cm": "170", "weight_kg": "72.6"}, format="json").json()
        self.assertEqual(data["latest"]["bmi"], 25.1)
        self.assertEqual(data["guidance"]["category_label"], "Overweight")
        self.assertEqual(data["guidance"]["healthy_weight_max_kg"], 72.0)
        self.assertTrue(any("1,000 more steps" in tip for tip in data["guidance"]["advice"]))
        tool = get_bmi(user, {})
        self.assertEqual(tool["category"], "Overweight")
        self.assertEqual(tool["healthy_weight_range_kg"], [53.5, 72.0])
