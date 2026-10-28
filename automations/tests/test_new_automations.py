import uuid
from datetime import datetime, timedelta, timezone as dt_timezone

from django.test import TestCase
from django.utils import timezone

from accounts.models import EmployeeProfile, User
from automations import services
from automations.models import AutomationSettings
from challenges.models import Challenge, ChallengeEnrollment
from fitness.models import DailyActivity, FitnessAccount, FitnessSource
from notifications.models import Notification
from nutrition.views import create_meal
from wellness.models import ReminderSettings

PW = "Strong-passphrase-123"


def user_with(email, **flags):
    user = User.objects.create_user(email, PW)
    ReminderSettings.objects.update_or_create(user=user, defaults={"time_zone": "UTC"})
    AutomationSettings.objects.update_or_create(user=user, defaults=flags)
    return user


class GoalNudgeTests(TestCase):
    def test_text_only_when_close_and_not_done(self):
        self.assertEqual(services.goal_nudge_text(8800, 10000),
                         "You're 1,200 steps from today's goal. An easy 12-minute walk would do it.")
        self.assertIsNone(services.goal_nudge_text(10000, 10000))
        self.assertIsNone(services.goal_nudge_text(2000, 10000))  # far away: no pressure
        self.assertIsNone(services.goal_nudge_text(0, 10000))

    def test_sends_once_from_fresh_steps_at_chosen_time(self):
        user = user_with("nudge@example.test", goal_nudge_enabled=True, goal_nudge_minute=19 * 60)
        now = timezone.now().replace(hour=19, minute=1, second=0, microsecond=0)
        source = FitnessSource.objects.create(user=user, installation_id=uuid.uuid4())
        FitnessAccount.objects.create(user=user, active_source=source, time_zone="UTC")
        DailyActivity.objects.create(user=user, source=source, local_date=now.date(), time_zone="UTC", steps=9000,
                                     metric_status={"steps": "available"}, observed_at=now)
        DailyActivity.objects.filter(user=user).update(synced_at=now - timedelta(minutes=30))
        self.assertEqual(services.send_goal_nudges(now - timedelta(hours=2)), 0)  # wrong time
        self.assertEqual(services.send_goal_nudges(now), 1)
        self.assertEqual(services.send_goal_nudges(now), 0)
        self.assertIn("1,000 steps", Notification.objects.get(user=user, kind="goal_nudge").body)

    def test_stale_steps_never_nudge(self):
        user = user_with("stale@example.test", goal_nudge_enabled=True)
        now = timezone.now().replace(hour=19, minute=0, second=0, microsecond=0)
        source = FitnessSource.objects.create(user=user, installation_id=uuid.uuid4())
        FitnessAccount.objects.create(user=user, active_source=source, time_zone="UTC")
        DailyActivity.objects.create(user=user, source=source, local_date=now.date(), time_zone="UTC", steps=9000,
                                     metric_status={"steps": "available"}, observed_at=now)
        DailyActivity.objects.filter(user=user).update(synced_at=now - timedelta(hours=5))
        self.assertEqual(services.send_goal_nudges(now), 0)


class ChallengeAlertTests(TestCase):
    def test_ending_tomorrow_and_new_challenges(self):
        user = user_with("alerts@example.test", challenge_alerts_enabled=True, send_minute=540)
        now = timezone.now().replace(hour=9, minute=0, second=0, microsecond=0)
        today = now.date()
        ending = Challenge.objects.create(slug="ending", title="Ending soon", start_date=today - timedelta(days=5),
                                          end_date=today + timedelta(days=1), target_steps=1000, reward_points=5, is_published=True)
        ChallengeEnrollment.objects.create(challenge=ending, user=user, joined_local_date=today - timedelta(days=5))
        Challenge.objects.create(slug="fresh", title="Fresh one", start_date=today, end_date=today + timedelta(days=9),
                                 target_steps=1000, reward_points=5, is_published=True)
        self.assertEqual(services.send_challenge_alerts(now), 2)
        self.assertEqual(services.send_challenge_alerts(now), 0)
        titles = set(Notification.objects.filter(user=user).values_list("title", flat=True))
        self.assertEqual(titles, {"Ending soon ends tomorrow", "New challenge you can join"})


class MealPromptTests(TestCase):
    def test_prompts_after_usual_time_with_same_as_usual(self):
        user = user_with("meals@example.test", meal_prompts_enabled=True)
        today = timezone.now().date()
        for back in range(1, 6):
            create_meal(user, {"client_request_id": uuid.uuid4(), "kind": "lunch", "time_zone": "UTC",
                               "occurred_at": datetime.combine(today - timedelta(days=back), datetime.min.time(),
                                                               tzinfo=dt_timezone.utc).replace(hour=13),
                               "items": [{"food_id": "indb:ASC113", "grams": 300}]})
        self.assertEqual(services.usual_meal_minutes(user), {"lunch": 13 * 60})
        at = datetime.combine(today, datetime.min.time(), tzinfo=dt_timezone.utc).replace(hour=13, minute=21)
        self.assertEqual(services.send_meal_prompts(at), 1)
        note = Notification.objects.get(user=user, kind="meal_prompt")
        self.assertEqual(note.data["meal_kind"], "lunch")
        self.assertIn("repeat_meal_id", note.data)
        self.assertEqual(services.send_meal_prompts(at), 0)

    def test_no_prompt_when_already_logged_or_too_little_history(self):
        user = user_with("meals2@example.test", meal_prompts_enabled=True)
        self.assertEqual(services.usual_meal_minutes(user), {})
        self.assertEqual(services.send_meal_prompts(), 0)


class PushPayloadTests(TestCase):
    def test_details_only_for_safe_kinds(self):
        from notifications.tasks import push_message
        user = User.objects.create_user("push@example.test", PW)
        meal = Notification.objects.create(user=user, kind="meal_prompt", title="Log your lunch", body="Had lunch?",
                                           event_key="a", data={"meal_kind": "lunch", "repeat_meal_id": 4})
        bmi = Notification.objects.create(user=user, kind="bmi_checkin", title="Weekly BMI check-in", body="weight",
                                          event_key="b", data={"x": "y"})
        meal_data = push_message("t" * 30, meal).data
        self.assertEqual((meal_data["title"], meal_data["x_meal_kind"], meal_data["x_repeat_meal_id"]),
                         ("Log your lunch", "lunch", "4"))
        bmi_data = push_message("t" * 30, bmi).data
        self.assertEqual(bmi_data["body"], "You have a new notification")
        self.assertNotIn("x_x", bmi_data)
