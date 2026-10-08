from unittest.mock import patch
from uuid import uuid4
from decimal import Decimal

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from nutrition.models import Meal


def selected_food(fdc_id):
    return {
        "fdc_id": fdc_id, "description": "Oatmeal", "data_type": "Foundation",
        "kcal_per_100g": 100, "protein_per_100g": 4,
        "carbohydrate_per_100g": None, "fat_per_100g": 2,
    }


class MealApiTests(APITestCase):
    def setUp(self):
        self.asha = User.objects.create_user("asha@infome.test", "Long-Passphrase-123")
        self.bilal = User.objects.create_user("bilal@infome.test", "Long-Passphrase-123")
        self.client.force_authenticate(self.asha)
        self.body = {
            "client_request_id": str(uuid4()), "kind": "breakfast",
            "occurred_at": "2026-10-01T20:00:00Z", "time_zone": "Asia/Kolkata",
            "notes": "Confirmed food and weighed portion",
            "items": [{"fdc_id": 12345, "grams": "150.0"}],
        }

    @patch("nutrition.views.food_details", side_effect=selected_food)
    def test_create_history_idempotency_and_local_date(self, lookup):
        response = self.client.post("/api/v1/nutrition/meals/", self.body, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["local_date"], "2026-10-02")
        self.assertEqual(response.data["total_kcal"], 150.0)
        self.assertEqual(response.data["protein_g"], 6.0)
        self.assertIsNone(response.data["carbohydrate_g"])
        self.assertEqual(response.data["items"][0]["food_name"], "Oatmeal")
        second = self.client.post("/api/v1/nutrition/meals/", self.body, format="json")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.data["id"], response.data["id"])
        self.assertEqual(lookup.call_count, 1)
        self.assertEqual(Meal.objects.count(), 1)
        history = self.client.get("/api/v1/nutrition/meals/")
        self.assertEqual(history.status_code, 200)
        self.assertEqual(len(history.data["results"]), 1)

    @patch("nutrition.views.food_details", side_effect=selected_food)
    def test_reused_key_with_changed_portion_conflicts(self, _lookup):
        self.assertEqual(self.client.post("/api/v1/nutrition/meals/", self.body, format="json").status_code, 201)
        changed = {**self.body, "items": [{"fdc_id": 12345, "grams": "200.0"}]}
        self.assertEqual(self.client.post("/api/v1/nutrition/meals/", changed, format="json").status_code, 409)
        self.assertEqual(Meal.objects.count(), 1)

    @patch("nutrition.views.food_details", side_effect=selected_food)
    def test_employee_isolation_and_own_delete(self, _lookup):
        meal_id = self.client.post("/api/v1/nutrition/meals/", self.body, format="json").data["id"]
        self.client.force_authenticate(self.bilal)
        self.assertEqual(self.client.get("/api/v1/nutrition/meals/").data["results"], [])
        self.assertEqual(self.client.get(f"/api/v1/nutrition/meals/{meal_id}/").status_code, 404)
        self.assertEqual(self.client.delete(f"/api/v1/nutrition/meals/{meal_id}/").status_code, 404)
        self.client.force_authenticate(self.asha)
        self.assertEqual(self.client.delete(f"/api/v1/nutrition/meals/{meal_id}/").status_code, 204)
        self.assertEqual(Meal.objects.count(), 0)

    def test_validation_and_authentication(self):
        invalid = {**self.body, "items": [{"fdc_id": 12345, "grams": "0"}]}
        self.assertEqual(self.client.post("/api/v1/nutrition/meals/", invalid, format="json").status_code, 400)
        invalid = {**self.body, "time_zone": "Not/A_Zone"}
        self.assertEqual(self.client.post("/api/v1/nutrition/meals/", invalid, format="json").status_code, 400)
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get("/api/v1/nutrition/meals/").status_code, 401)

    def test_iva_reads_only_own_meal_totals_and_keeps_missing_macros_null(self):
        from ai_service.tools import read_context
        now = timezone.now()
        Meal.objects.create(user=self.asha, client_request_id=uuid4(), payload_hash="a" * 64,
                            kind="lunch", occurred_at=now, local_date=now.date(), time_zone="UTC",
                            total_kcal=Decimal("250.00"), protein_g=Decimal("10.00"),
                            carbohydrate_g=None, fat_g=Decimal("5.00"))
        asha, sources = read_context(self.asha, "How many calories did I eat today?")
        self.assertIn("nutrition", sources)
        self.assertEqual(asha["nutrition"]["estimated_food_kcal"], 250.0)
        self.assertIsNone(asha["nutrition"]["carbohydrate_g"])
        bilal, _ = read_context(self.bilal, "How many calories did I eat today?")
        self.assertEqual(bilal["nutrition"]["status"], "no_data")

    def test_meal_admin_details_are_superuser_only(self):
        business_admin = User.objects.create_user("admin@infome.test", "Long-Passphrase-123",
                                                   role="admin", is_staff=True)
        self.client.force_authenticate(user=None)
        self.client.force_login(business_admin)
        self.assertEqual(self.client.get("/admin/nutrition/meal/").status_code, 403)
        root = User.objects.create_superuser("root@infome.test", "Long-Passphrase-123")
        self.client.force_login(root)
        self.assertEqual(self.client.get("/admin/nutrition/meal/").status_code, 200)

    @override_settings(USDA_FDC_API_KEY="test_key")
    @patch("ai_service.nutrition.urllib.request.urlopen")
    def test_usda_detail_reads_nested_nutrients(self, urlopen):
        import json
        from io import BytesIO
        from ai_service.nutrition import food_details

        detail = {"fdcId": 12345, "description": "Oatmeal", "dataType": "Foundation", "foodNutrients": [
            {"nutrient": {"id": 2047, "unitName": "kcal"}, "amount": 100},
            {"nutrient": {"id": 1003, "unitName": "g"}, "amount": 4},
            {"nutrient": {"id": 1004, "unitName": "g"}, "amount": 2},
        ]}
        urlopen.return_value.__enter__.return_value = BytesIO(json.dumps(detail).encode())
        found = food_details(12345)
        self.assertEqual(found["kcal_per_100g"], 100)
        self.assertEqual(found["protein_per_100g"], 4)
        self.assertIsNone(found["carbohydrate_per_100g"])
