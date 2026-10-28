import json
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from ai_service.nutrition import NutritionUnavailable
from assistant.models import AssistantProposal
from assistant.services import validate_draft
from nutrition.models import Meal
from wellness.models import HydrationLog

MSG = "/api/v1/assistant/message/"
PW = "Strong-passphrase-123"
FOOD = {"fdc_id": 11, "description": "Idli, steamed", "data_type": "Survey (FNDDS)", "kcal_per_100g": 130.0}
DETAILS = {"fdc_id": 11, "description": "Idli, steamed", "data_type": "Survey (FNDDS)", "kcal_per_100g": 130.0,
           "protein_per_100g": 4.0, "carbohydrate_per_100g": 28.0, "fat_per_100g": 0.4}
CHAT = ("Keep moving!", ["fitness"])


def water(ml=250):
    return json.dumps({"action": "log_water", "volume_ml": ml})


def meal(items=None):
    return json.dumps({"action": "log_meal", "meal_kind": "breakfast",
                       "items": items or [{"food": "idli", "grams": 80}]})


class Base(APITestCase):
    def setUp(self):
        cache.clear()  # the "ai" throttle counter lives in the cache and would leak between tests
        self.user = User.objects.create_user("assist@example.test", PW)
        self.other = User.objects.create_user("assist2@example.test", PW)
        self.client.force_authenticate(self.user)

    def say(self, text="I just drank a glass of water"):
        return self.client.post(MSG, {"message": text, "history": []}, format="json")


class WaterFlowTests(Base):
    @patch("assistant.services.complete", return_value=water())
    def test_propose_confirm_idempotent_and_undo(self, _):
        res = self.say()
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["type"], "proposal")
        self.assertEqual(HydrationLog.objects.count(), 0)  # nothing saved until confirmed
        pid = res.data["proposal"]["id"]
        done = self.client.post(f"/api/v1/assistant/proposals/{pid}/confirm/")
        self.assertEqual(done.data["status"], "confirmed")
        self.assertEqual(HydrationLog.objects.get().volume_ml, 250)
        self.assertEqual(self.client.post(f"/api/v1/assistant/proposals/{pid}/confirm/").status_code, 409)
        self.assertEqual(HydrationLog.objects.count(), 1)  # a double tap never double-logs
        undone = self.client.post(f"/api/v1/assistant/proposals/{pid}/undo/")
        self.assertEqual(undone.data["status"], "undone")
        self.assertEqual(HydrationLog.objects.count(), 0)

    @patch("assistant.services.complete", return_value=water())
    def test_cancel_logs_nothing(self, _):
        pid = self.say().data["proposal"]["id"]
        self.assertEqual(self.client.post(f"/api/v1/assistant/proposals/{pid}/cancel/").data["status"], "cancelled")
        self.assertEqual(self.client.post(f"/api/v1/assistant/proposals/{pid}/confirm/").status_code, 409)
        self.assertEqual(HydrationLog.objects.count(), 0)

    @patch("assistant.services.complete", return_value=water())
    def test_expired_proposal_is_not_saved(self, _):
        pid = self.say().data["proposal"]["id"]
        AssistantProposal.objects.update(expires_at=timezone.now() - timedelta(minutes=1))
        res = self.client.post(f"/api/v1/assistant/proposals/{pid}/confirm/")
        self.assertEqual(res.data["status"], "expired")
        self.assertEqual(HydrationLog.objects.count(), 0)

    @patch("assistant.services.complete", return_value=water())
    def test_other_users_cannot_answer_my_proposal(self, _):
        pid = self.say().data["proposal"]["id"]
        self.client.force_authenticate(self.other)
        for action in ("confirm", "cancel", "undo"):
            self.assertEqual(self.client.post(f"/api/v1/assistant/proposals/{pid}/{action}/").status_code, 404)
        self.assertEqual(HydrationLog.objects.count(), 0)


class RoutingAndSafetyTests(Base):
    @patch("assistant.services.answer_chat", return_value=CHAT)
    @patch("assistant.services.complete")
    def test_ordinary_question_skips_the_drafting_call(self, extract, _):
        res = self.say("How many steps do I have today?")
        self.assertEqual(res.data["type"], "answer")
        extract.assert_not_called()

    @patch("assistant.services.answer_chat", return_value=CHAT)
    @patch("assistant.services.complete")
    def test_anything_phrased_as_a_question_is_never_drafted(self, extract, _):
        for text in ("Did I drink enough water today?", "What should I have for lunch?", "How many ml in a glass?"):
            self.assertEqual(self.say(text).data["type"], "answer", text)
        extract.assert_not_called()

    @patch("assistant.services.answer_chat", return_value=CHAT)
    @patch("assistant.services.complete", return_value=json.dumps({"action": "none"}))
    def test_model_can_still_decline_a_statement(self, *_):
        res = self.say("I will drink more water tomorrow")
        self.assertEqual(res.data["type"], "answer")
        self.assertEqual(AssistantProposal.objects.count(), 0)

    @patch("assistant.services.answer_chat", return_value=CHAT)
    def test_bad_model_output_never_creates_a_proposal(self, _):
        for bad in ("not json at all", water(5000), json.dumps({"action": "delete_account"}),
                    json.dumps({"action": "log_water", "volume_ml": "lots"}), json.dumps(["log_water"]),
                    json.dumps({"action": "log_meal", "items": []})):
            with patch("assistant.services.complete", return_value=bad):
                self.assertEqual(self.say().data["type"], "answer", bad)
        self.assertEqual(AssistantProposal.objects.count(), 0)

    @patch("assistant.services.answer_chat", return_value=CHAT)
    @patch("assistant.services.complete", side_effect=Exception("provider down"))
    def test_model_failure_falls_back_to_chat(self, *_):
        self.assertEqual(self.say().data["type"], "answer")

    @patch("assistant.services.complete", return_value="```json\n" + water(500) + "\n```")
    def test_accepts_fenced_json(self, _):
        self.assertEqual(self.say("drank a bottle").data["proposal"]["summary"], "Log 500 ml of water")

    def test_validate_draft_rules(self):
        self.assertIsNone(validate_draft({"action": "log_meal", "items": [{"food": "x", "grams": 5000}]}, 9))
        self.assertEqual(validate_draft({"action": "log_meal", "items": [{"food": "tea", "grams": 200}]}, 9)["meal_kind"], "breakfast")
        self.assertEqual(validate_draft({"action": "log_meal", "meal_kind": "bogus", "items": [{"food": "tea", "grams": 200}]}, 20)["meal_kind"], "dinner")

    def test_requires_authentication(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.say().status_code, 401)


class MealFlowTests(Base):
    @patch("nutrition.foods.food_details", return_value=DETAILS)
    @patch("assistant.services.search_foods", return_value=[FOOD])
    @patch("assistant.services.complete", return_value=meal())
    def test_meal_is_resolved_from_usda_confirmed_then_undone(self, *_):
        res = self.say("I had two idlis for breakfast")
        proposal = res.data["proposal"]
        self.assertEqual(proposal["summary"], "Log breakfast: about 104 kcal")  # 130 kcal/100 g x 80 g, from the database
        self.assertIn("Idli, steamed", proposal["details"][0])
        self.assertEqual(Meal.objects.count(), 0)
        done = self.client.post(f"/api/v1/assistant/proposals/{proposal['id']}/confirm/")
        self.assertEqual(done.data["status"], "confirmed")
        saved = Meal.objects.get()
        self.assertEqual(float(saved.total_kcal), 104.0)
        self.assertEqual(saved.items.get().fdc_id, 11)
        self.assertEqual(self.client.post(f"/api/v1/assistant/proposals/{proposal['id']}/confirm/").status_code, 409)
        self.assertEqual(Meal.objects.count(), 1)
        self.client.post(f"/api/v1/assistant/proposals/{proposal['id']}/undo/")
        self.assertEqual(Meal.objects.count(), 0)

    @patch("assistant.services.answer_chat", return_value=CHAT)
    @patch("assistant.services.search_foods", return_value=[])
    @patch("assistant.services.complete", return_value=meal())
    def test_unmatched_foods_make_no_proposal(self, *_):
        self.assertEqual(self.say("I had idli").data["type"], "answer")

    @patch("assistant.services.answer_chat", return_value=CHAT)
    @patch("assistant.services.search_foods", side_effect=NutritionUnavailable())
    @patch("assistant.services.complete", return_value=meal())
    def test_food_lookup_outage_falls_back_to_chat(self, *_):
        self.assertEqual(self.say("I had idli").data["type"], "answer")

    @patch("nutrition.foods.food_details", return_value=DETAILS)
    @patch("assistant.services.search_foods", side_effect=[[FOOD], []])
    @patch("assistant.services.complete", return_value=meal([{"food": "idli", "grams": 80}, {"food": "mystery", "grams": 50}]))
    def test_partly_matched_meal_lists_what_was_missed(self, *_):
        proposal = self.say("idli and mystery for breakfast").data["proposal"]
        self.assertIn("Could not find: mystery", proposal["details"])
        self.assertEqual(len(AssistantProposal.objects.get().payload["items"]), 1)
