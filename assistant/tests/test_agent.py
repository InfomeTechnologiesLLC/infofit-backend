"""IVA with tools: own-data lookups, drafts that need confirmation, saved history and streaming."""
import json
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import EmployeeProfile, User
from assistant.models import AssistantProposal, ChatTurn
from challenges.models import Challenge, ChallengeEnrollment
from fitness.models import DailyActivity, FitnessAccount, FitnessSource
from nutrition.models import Meal
from nutrition.views import create_meal
from wellness.models import HydrationLog, ProgramCheckIn, ProgramEnrollment, ReminderSettings, WellnessProgram

PW = "Strong-passphrase-123"
CHAT = "/api/v1/assistant/chat/"


def call(name, **args):
    return [{"functionCall": {"name": name, "args": args}}]


def text(value):
    return [{"text": value}]


class AgentBase(APITestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("agent@example.test", PW, first_name="Asha")
        self.other = User.objects.create_user("agent2@example.test", PW)
        self.client.force_authenticate(self.user)
        for user, steps in ((self.user, 6500), (self.other, 12345)):
            source = FitnessSource.objects.create(user=user, installation_id=uuid.uuid4())
            FitnessAccount.objects.create(user=user, active_source=source, time_zone="UTC")
            DailyActivity.objects.create(user=user, source=source, local_date=timezone.now().date(), time_zone="UTC",
                                         steps=steps, metric_status={"steps": "available"}, observed_at=timezone.now())

    def say(self, message="How am I doing today?"):
        return self.client.post(CHAT, {"message": message}, format="json")

    def proposal(self, response):
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["type"], "proposal")
        return AssistantProposal.objects.get(pk=response.data["proposal"]["id"])

    def answer(self, pk, action):
        return self.client.post(f"/api/v1/assistant/proposals/{pk}/{action}/")


class ReadToolTests(AgentBase):
    @patch("assistant.agent.generate", side_effect=[call("get_today_summary"), text("3,500 steps to go!")])
    def test_model_looks_up_own_data_and_never_sees_others(self, generate):
        response = self.say("Did I hit my goal?")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["answer"], "3,500 steps to go!")
        self.assertEqual(response.data["used_context"], ["get_today_summary"])
        contents = generate.call_args_list[1].args[1]
        tool_reply = json.dumps(contents[-1])
        self.assertIn('"steps": 6500', tool_reply)
        self.assertIn('"remaining_steps_today": 3500', tool_reply)
        self.assertNotIn("12345", json.dumps(contents))
        system = generate.call_args_list[0].args[0]
        self.assertIn("Asha", system)
        self.assertNotIn("agent@example.test", system)

    @patch("assistant.agent.generate", side_effect=[text("Hi Asha!"), text("Still here.")])
    def test_history_is_saved_and_sent_back(self, generate):
        self.say("Hello")
        self.say("Are you there?")
        contents = generate.call_args_list[1].args[1]
        self.assertEqual([c["parts"][0]["text"] for c in contents], ["Hello", "Hi Asha!", "Are you there?"])
        history = self.client.get("/api/v1/assistant/history/").data["turns"]
        self.assertEqual([t["content"] for t in history], ["Hello", "Hi Asha!", "Are you there?", "Still here."])
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get("/api/v1/assistant/history/").data["turns"], [])
        self.client.force_authenticate(self.user)
        self.client.delete("/api/v1/assistant/history/")
        self.assertFalse(ChatTurn.objects.filter(user=self.user).exists())

    @patch("assistant.agent.generate", side_effect=[call("get_step_history", days=999), text("Sorry")])
    def test_bad_arguments_are_reported_to_the_model_not_repaired(self, generate):
        self.say("Show my last 999 days")
        reply = json.dumps(generate.call_args_list[1].args[1][-1])
        self.assertIn("between 1 and 31", reply)

    def test_provider_failure_is_a_clean_503(self):
        from ai_service.provider import AiUnavailable
        with patch("assistant.agent.generate", side_effect=AiUnavailable()):
            self.assertEqual(self.say().status_code, 503)


class ActionToolTests(AgentBase):
    @patch("assistant.agent.generate", side_effect=[call("log_water", volume_ml=500), text("Please confirm below.")])
    def test_water_is_drafted_then_confirmed_and_undone(self, _):
        proposal = self.proposal(self.say("I drank a bottle of water"))
        self.assertFalse(HydrationLog.objects.exists())
        self.assertEqual(self.answer(proposal.pk, "confirm").data["status"], "confirmed")
        self.assertEqual(HydrationLog.objects.get(user=self.user).volume_ml, 500)
        self.answer(proposal.pk, "undo")
        self.assertFalse(HydrationLog.objects.exists())

    @patch("assistant.agent.generate", side_effect=[call("log_water", volume_ml=250) + call("set_step_goal", daily_step_goal=5000), text("")])
    def test_only_one_change_per_message_and_empty_text_gets_a_default(self, _):
        response = self.say("Water and lower my goal")
        self.assertEqual(response.data["answer"], "Here is what I understood. Please check it, then confirm.")
        self.assertEqual(AssistantProposal.objects.filter(user=self.user).count(), 1)

    @patch("assistant.agent.generate", side_effect=[call("log_meal", meal_kind="breakfast", items=[{"food": "idli", "servings": 2}, {"food": "sambar", "grams": 150}]), text("Check this.")])
    def test_meal_uses_indian_foods_and_servings(self, _):
        proposal = self.proposal(self.say("I had 2 idlis and sambar"))
        self.assertEqual([i["food_id"] for i in proposal.payload["items"]], ["indb:ASC144", "indb:ASC167"])
        self.assertIn("2 idlis", proposal.details[0])
        self.answer(proposal.pk, "confirm")
        meal = Meal.objects.get(user=self.user)
        self.assertEqual(meal.kind, "breakfast")
        self.assertEqual({i.food_source for i in meal.items.all()}, {"indb"})

    @patch("assistant.agent.generate", side_effect=[call("set_step_goal", daily_step_goal=8000), text("Confirm?")])
    def test_step_goal_change_and_undo_restores_previous(self, _):
        proposal = self.proposal(self.say("Set my goal to 8000"))
        self.answer(proposal.pk, "confirm")
        self.assertEqual(EmployeeProfile.objects.get(user=self.user).daily_step_goal, 8000)
        self.answer(proposal.pk, "undo")
        self.assertEqual(EmployeeProfile.objects.get(user=self.user).daily_step_goal, 10000)

    def test_join_challenge_and_reminder_change(self):
        today = timezone.now().date()
        challenge = Challenge.objects.create(slug="walk", title="Walk Week", start_date=today - timedelta(days=1),
                                             end_date=today + timedelta(days=5), target_steps=50000, reward_points=10,
                                             is_published=True)
        with patch("assistant.agent.generate", side_effect=[call("join_challenge", challenge_id=challenge.pk), text("Join?")]):
            proposal = self.proposal(self.say("Join the walk week"))
        self.answer(proposal.pk, "confirm")
        self.assertTrue(ChallengeEnrollment.objects.filter(user=self.user, challenge=challenge).exists())
        self.answer(proposal.pk, "undo")
        self.assertFalse(ChallengeEnrollment.objects.filter(user=self.user, challenge=challenge).exists())

        with patch("assistant.agent.generate", side_effect=[call("set_reminder", reminder="water", enabled=True, interval_minutes=60), text("OK?")]):
            proposal = self.proposal(self.say("Remind me to drink water every hour"))
        self.answer(proposal.pk, "confirm")
        settings = ReminderSettings.objects.get(user=self.user)
        self.assertTrue(settings.water_enabled)
        self.assertEqual(settings.water_interval_minutes, 60)
        self.answer(proposal.pk, "undo")
        settings.refresh_from_db()
        self.assertFalse(settings.water_enabled)
        self.assertEqual(settings.water_interval_minutes, 90)

    def test_personal_plan_is_private_and_can_be_checked_in(self):
        days = [f"Walk {10 + i} minutes" for i in range(7)]
        with patch("assistant.agent.generate", side_effect=[call("create_weekly_plan", title="Gentle start", days=days), text("Start it?")]):
            proposal = self.proposal(self.say("Make me a walking plan"))
        self.answer(proposal.pk, "confirm")
        plan = WellnessProgram.objects.get(owner=self.user)
        programs = self.client.get("/api/v1/wellness/programs/").data
        self.assertEqual(programs[0]["title"], "Gentle start")
        self.assertTrue(programs[0]["personal"])
        self.assertEqual(programs[0]["today_action"], "Walk 10 minutes")
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get("/api/v1/wellness/programs/").data, [])
        self.assertEqual(self.client.post(f"/api/v1/wellness/programs/{plan.pk}/join/").status_code, 404)
        self.client.force_authenticate(self.user)
        with patch("assistant.agent.generate", side_effect=[call("check_in_program", program_id=plan.pk), text("Check in?")]):
            proposal = self.proposal(self.say("I did today's walk"))
        self.answer(proposal.pk, "confirm")
        self.assertEqual(ProgramCheckIn.objects.filter(enrollment__user=self.user).count(), 1)

    def test_preferences_are_remembered_and_used(self):
        with patch("assistant.agent.generate", side_effect=[call("remember_preference", key="diet", value="Vegetarian"), text("Noted?")]):
            proposal = self.proposal(self.say("I'm vegetarian"))
        self.answer(proposal.pk, "confirm")
        self.assertEqual(self.client.get("/api/v1/assistant/preferences/").data, {"diet": "vegetarian"})
        with patch("assistant.agent.generate", return_value=text("Try a paneer wrap.")) as generate:
            self.say("Suggest a lunch")
        self.assertIn('"diet": "vegetarian"', generate.call_args.args[0])
        self.client.patch("/api/v1/assistant/preferences/", {"diet": "", "cuisine": "South Indian"}, format="json")
        self.assertEqual(self.client.get("/api/v1/assistant/preferences/").data, {"cuisine": "South Indian"})

    def test_repeat_meal(self):
        create_meal(self.user, {"client_request_id": uuid.uuid4(), "kind": "lunch", "time_zone": "UTC",
                                "occurred_at": timezone.now() - timedelta(days=1),
                                "items": [{"food_id": "indb:ASC113", "grams": 300}]})
        with patch("assistant.agent.generate", side_effect=[call("repeat_meal", meal_kind="lunch", when="yesterday"), text("Same again?")]):
            proposal = self.proposal(self.say("Same lunch as yesterday"))
        self.answer(proposal.pk, "confirm")
        self.assertEqual(Meal.objects.filter(user=self.user, kind="lunch").count(), 2)


class StreamTests(AgentBase):
    def test_stream_sends_text_pieces_then_done(self):
        rounds = iter([call("get_today_summary"), text("Nearly ") + text("there!")])
        with patch("assistant.agent.stream", side_effect=lambda *a, **k: iter(next(rounds))):
            response = self.client.post("/api/v1/assistant/stream/", {"message": "Steps?"}, format="json")
            body = b"".join(response.streaming_content).decode()
        self.assertEqual(response["Content-Type"], "text/event-stream")
        events = [block for block in body.split("\n\n") if block]
        self.assertEqual([e.split("\n")[0] for e in events], ["event: delta", "event: delta", "event: done"])
        done = json.loads(events[-1].split("data: ", 1)[1])
        self.assertEqual(done["answer"], "Nearly there!")

    def test_stream_reports_errors_as_an_event(self):
        from ai_service.provider import AiUnavailable
        with patch("assistant.agent.stream", side_effect=AiUnavailable()):
            response = self.client.post("/api/v1/assistant/stream/", {"message": "Hi"}, format="json")
            body = b"".join(response.streaming_content).decode()
        self.assertIn("event: error", body)
        self.assertIn("unavailable", body)


class StreamAcceptHeaderTests(APITestCase):
    """The Android app sends Accept: text/event-stream; DRF must not answer 406."""

    def setUp(self):
        self.user = User.objects.create_user(email="stream@example.test", password="Strong-Passphrase-123")
        self.client.force_authenticate(self.user)

    @override_settings(GEMINI_API_KEY="")
    def test_event_stream_accept_header_reaches_the_view(self):
        response = self.client.post("/api/v1/assistant/stream/", {"message": "hello"}, format="json",
                                    HTTP_ACCEPT="text/event-stream")
        self.assertEqual(response.status_code, 200)
        body = b"".join(response.streaming_content).decode()
        self.assertIn("event: error", body)  # no AI key in tests, so IVA reports it is unavailable

    def test_validation_errors_are_still_readable_json(self):
        response = self.client.post("/api/v1/assistant/stream/", {"message": ""}, format="json",
                                    HTTP_ACCEPT="text/event-stream")
        self.assertEqual(response.status_code, 400)
        self.assertIn(b'"error"', response.content)
