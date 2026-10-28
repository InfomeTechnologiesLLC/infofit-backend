"""Indian food database, combined search, meals with local foods, frequent and repeated meals."""
import uuid
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch

from django.core.cache import cache
from django.utils import timezone
from PIL import Image
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APITestCase

from accounts.models import User
from nutrition import foods
from nutrition.models import FoodItem, Meal

PW = "Strong-passphrase-123"
USDA_ROW = {"fdc_id": 99, "description": "Biryani with chicken", "data_type": "Survey (FNDDS)", "kcal_per_100g": 180.0}


class FoodDatabaseTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("foods@example.test", PW)
        self.client.force_authenticate(self.user)

    def test_bundled_data_is_loaded_and_oil_inflated_recipes_are_excluded(self):
        self.assertGreater(FoodItem.objects.filter(source="indb").count(), 800)
        self.assertGreater(FoodItem.objects.filter(source="ifct").count(), 500)
        self.assertFalse(FoodItem.objects.filter(source="indb", fat_per_100g__gt=35).exists())

    def test_everyday_names_find_the_food_people_mean(self):
        self.assertEqual(foods.local_search("2 idlis")[0]["food_id"], "indb:ASC144")
        self.assertEqual(foods.local_search("rice")[0]["description"], "Boiled rice (Uble chawal)")
        self.assertEqual(foods.local_search("eggs")[0]["food_id"], "ifct:M004")
        self.assertEqual(foods.local_search("rasam")[0]["description"][:5], "Rasam")
        self.assertNotIn("powder", foods.local_search("rasam")[0]["description"].lower())

    @patch("nutrition.foods.usda_search", return_value=[USDA_ROW])
    def test_full_local_matches_then_usda_then_partial(self, _):
        names = [row["description"] for row in foods.search("chicken biryani")]
        self.assertEqual(names[0], "Biryani with chicken")  # no full local match, so USDA comes first
        self.assertIn("biryani", names[1].lower())

    def test_local_results_still_work_without_usda(self):
        response = self.client.get("/api/v1/ai/foods/", {"q": "masala dosa", "v": "2"})
        self.assertEqual(response.status_code, 200)
        first = response.data["results"][0]
        self.assertEqual(first["food_id"], "indb:ASC146")
        self.assertIsNone(first["fdc_id"])
        self.assertEqual(first["serving_unit"], "dosa")

    def test_meal_with_local_food_is_priced_on_the_server(self):
        response = self.client.post("/api/v1/nutrition/meals/", {
            "client_request_id": str(uuid.uuid4()), "kind": "breakfast", "occurred_at": timezone.now().isoformat(),
            "time_zone": "Asia/Kolkata", "items": [{"food_id": "indb:ASC144", "grams": 50}]}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        idli = FoodItem.objects.get(source="indb", code="ASC144")
        self.assertAlmostEqual(response.data["total_kcal"], float(idli.kcal_per_100g * Decimal("0.5")), places=1)
        self.assertEqual(response.data["items"][0]["food_id"], "indb:ASC144")

    def test_bad_food_references_are_rejected(self):
        base = {"client_request_id": str(uuid.uuid4()), "kind": "lunch", "occurred_at": timezone.now().isoformat(),
                "time_zone": "UTC"}
        for items in ([{"food_id": "indb:NOPE", "grams": 10}], [{"food_id": "bad", "grams": 10}],
                      [{"food_id": "indb:ASC144", "fdc_id": 1, "grams": 10}], [{"grams": 10}]):
            response = self.client.post("/api/v1/nutrition/meals/", {**base, "items": items}, format="json")
            self.assertEqual(response.status_code, 400, items)
        self.assertFalse(Meal.objects.exists())

    def test_frequent_and_repeat(self):
        for _ in range(2):
            self.client.post("/api/v1/nutrition/meals/", {
                "client_request_id": str(uuid.uuid4()), "kind": "breakfast", "occurred_at": timezone.now().isoformat(),
                "time_zone": "UTC", "items": [{"food_id": "indb:ASC144", "grams": 75}]}, format="json")
        frequent = self.client.get("/api/v1/nutrition/meals/frequent/").data["results"]
        self.assertEqual(frequent[0]["times_logged"], 2)
        request_id = str(uuid.uuid4())
        first = self.client.post(f"/api/v1/nutrition/meals/{frequent[0]['id']}/repeat/", {"client_request_id": request_id}, format="json")
        again = self.client.post(f"/api/v1/nutrition/meals/{frequent[0]['id']}/repeat/", {"client_request_id": request_id}, format="json")
        self.assertEqual(first.status_code, 201)
        self.assertEqual(again.status_code, 200)  # same request id: saved once
        self.assertEqual(Meal.objects.filter(user=self.user).count(), 3)
        other = User.objects.create_user("foods2@example.test", PW)
        self.client.force_authenticate(other)
        self.assertEqual(self.client.post(f"/api/v1/nutrition/meals/{frequent[0]['id']}/repeat/",
                                          {"client_request_id": str(uuid.uuid4())}, format="json").status_code, 404)


class LensItemsTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("lens@example.test", PW)
        self.client.force_authenticate(self.user)

    def photo(self):
        buffer = BytesIO()
        Image.new("RGB", (24, 24), color="white").save(buffer, format="JPEG")
        return SimpleUploadedFile("meal.jpg", buffer.getvalue(), content_type="image/jpeg")

    @patch("ai_service.views.complete_json", return_value={"is_food": True, "description": "Idli with sambar.",
                                                          "items": [{"food": "idli", "grams": 80}, {"food": "sambar", "grams": 9999}]})
    def test_one_tap_lens_matches_items_and_bounds_grams(self, _):
        response = self.client.post("/api/v1/ai/lens/?v=2", {"image": self.photo()}, format="multipart")
        self.assertEqual(response.status_code, 200)
        items = response.data["items"]
        self.assertEqual(items[0]["match"]["food_id"], "indb:ASC144")
        self.assertEqual(items[0]["grams"], 80)
        self.assertEqual(items[1]["grams"], items[1]["match"]["serving_g"])  # implausible estimate replaced
        self.assertTrue(response.data["nutrition_available"])

    @patch("ai_service.views.complete_json", return_value={"is_food": False, "description": "A keyboard.", "items": [{"food": "keys"}]})
    def test_not_food_returns_no_items(self, _):
        response = self.client.post("/api/v1/ai/lens/?v=2", {"image": self.photo()}, format="multipart")
        self.assertEqual(response.data["items"], [])
        self.assertFalse(response.data["is_food"])
