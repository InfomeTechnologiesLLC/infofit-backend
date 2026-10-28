import uuid
from datetime import date, datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APITestCase

from accounts.models import Department, EmployeeProfile, Role, User
from automations import services
from automations.models import AutomationLog, AutomationSettings, StepGoalSuggestion
from automations.tasks import run_scheduled_automations
from challenges.models import Challenge
from fitness.models import DailyActivity, FitnessAccount, FitnessSource
from notifications.models import Notification
from nutrition.models import Meal
from wellness.models import BodyMeasurement, HydrationLog, ReminderSettings

PW = "Strong-passphrase-123"
MONDAY = date(2026, 10, 26)
NOW = datetime(2026, 10, 26, 9, 0, tzinfo=dt_timezone.utc)  # Monday 09:00 UTC


def make_user(email, role=Role.EMPLOYEE):
    user = User.objects.create_user(email, PW, role=role)
    ReminderSettings.objects.update_or_create(user=user, defaults={"time_zone": "UTC"})
    return user


def add_steps(user, day, steps):
    account, _ = FitnessAccount.objects.get_or_create(user=user, defaults={"time_zone": "UTC"})
    if not account.active_source_id:
        account.active_source = FitnessSource.objects.create(user=user, installation_id=uuid.uuid4())
        account.save()
    DailyActivity.objects.create(user=user, source=account.active_source, local_date=day, time_zone="UTC", steps=steps,
                                 metric_status={"steps": "available"}, observed_at=NOW)


def add_weeks(user, per_week_steps, weeks_back=(14, 7)):
    for back in weeks_back:
        monday = MONDAY - timedelta(days=back)
        for i in range(7):
            add_steps(user, monday + timedelta(days=i), per_week_steps)


def enable(user, **flags):
    AutomationSettings.objects.update_or_create(user=user, defaults=flags)


class SettingsApiTests(APITestCase):
    def setUp(self):
        self.user = make_user("auto@example.test")
        self.client.force_authenticate(self.user)

    def test_defaults_off_patch_and_validation(self):
        data = self.client.get("/api/v1/automations/settings/").data
        self.assertFalse(data["daily_coach_enabled"] or data["weekly_review_enabled"] or data["reengagement_enabled"])
        patched = self.client.patch("/api/v1/automations/settings/", {"daily_coach_enabled": True, "send_minute": 600}, format="json")
        self.assertTrue(patched.data["daily_coach_enabled"])
        self.assertEqual(patched.data["send_minute"], 600)
        self.assertEqual(self.client.patch("/api/v1/automations/settings/", {"send_minute": 2000}, format="json").status_code, 400)

    def test_requires_auth(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get("/api/v1/automations/settings/").status_code, 401)


class DailyCoachTests(TestCase):
    def setUp(self):
        self.user = make_user("daily@example.test")
        enable(self.user, daily_coach_enabled=True)

    @patch("automations.services.complete", side_effect=Exception("provider down"))
    def test_sends_once_in_slot_with_template_fallback(self, _):
        self.assertEqual(services.send_daily_coach(NOW), 1)
        self.assertEqual(services.send_daily_coach(NOW + timedelta(minutes=2)), 0)  # same day: idempotent
        note = Notification.objects.get(kind="coach_nudge")
        self.assertIn("short walk", note.body)

    @patch("automations.services.complete", return_value="  Take a brisk walk\nafter lunch.  ")
    def test_uses_ai_wording_when_available_and_ignores_other_times(self, _):
        self.assertEqual(services.send_daily_coach(NOW.replace(hour=14)), 0)  # outside the 09:00 slot
        services.send_daily_coach(NOW)
        self.assertEqual(Notification.objects.get().body, "Take a brisk walk after lunch.")

    def test_disabled_and_inactive_users_get_nothing(self):
        enable(self.user, daily_coach_enabled=False)
        self.assertEqual(services.send_daily_coach(NOW), 0)

    @patch("automations.services.complete", side_effect=Exception("down"))
    def test_daily_cap_blocks_a_third_message(self, _):
        AutomationLog.objects.create(user=self.user, kind="weekly_review", local_date=MONDAY)
        AutomationLog.objects.create(user=self.user, kind="reengagement", local_date=MONDAY)
        self.assertEqual(services.send_daily_coach(NOW), 0)

    @patch("automations.services.complete", side_effect=Exception("down"))
    def test_respects_local_time_zone(self, _):
        ReminderSettings.objects.filter(user=self.user).update(time_zone="Asia/Kolkata")
        self.assertEqual(services.send_daily_coach(datetime(2026, 10, 26, 3, 30, tzinfo=dt_timezone.utc)), 1)  # 09:00 IST


class WeeklyReviewTests(TestCase):
    def setUp(self):
        self.user = make_user("weekly@example.test")
        enable(self.user, weekly_review_enabled=True)

    @patch("automations.services.complete", side_effect=Exception("down"))
    def test_review_uses_previous_week_and_never_mentions_weight(self, _):
        monday = MONDAY - timedelta(days=7)
        for i in range(7):
            add_steps(self.user, monday + timedelta(days=i), 11000 if i < 3 else 4000)
        BodyMeasurement.objects.create(user=self.user, height_cm="170", weight_kg="70", bmi="24.2",
                                       local_date=monday, time_zone="UTC")
        data = services.review_facts(self.user, MONDAY)
        self.assertIn("reached the daily step goal on 3 days", data["fallback"])
        self.assertIn("4 days", data["fallback"])  # small goal: one more day than last week
        self.assertNotIn("weight", " ".join(data["facts"]).lower() + data["fallback"].lower())
        self.assertNotIn("bmi", " ".join(data["facts"]).lower())
        self.assertEqual(services.send_weekly_reviews(NOW), 1)
        self.assertEqual(Notification.objects.filter(kind="weekly_review").count(), 1)

    @patch("automations.services.complete", side_effect=Exception("down"))
    def test_only_on_mondays_and_no_data_is_not_called_inactivity(self, _):
        self.assertEqual(services.send_weekly_reviews(NOW + timedelta(days=1)), 0)
        data = services.review_facts(self.user, MONDAY)
        self.assertIn("No step data synced", data["facts"][0])
        self.assertNotIn("inactive", data["fallback"].lower())


class StepGoalTests(TestCase):
    def setUp(self):
        self.user = make_user("goal@example.test")

    def test_raises_after_two_strong_weeks_and_lowers_after_two_weak_ones(self):
        add_weeks(self.user, 12000)
        self.assertEqual(services.propose_step_goal(self.user, MONDAY)[0], 11000)
        DailyActivity.objects.all().delete()
        add_weeks(self.user, 2000)
        self.assertEqual(services.propose_step_goal(self.user, MONDAY)[0], 8500)

    def test_just_under_goal_every_day_counts_as_weak_and_sparse_data_gives_nothing(self):
        add_weeks(self.user, 9000)  # goal is 10,000, so 0 days met in both weeks
        self.assertEqual(services.propose_step_goal(self.user, MONDAY)[0], 8500)
        user2 = make_user("sparse@example.test")
        for i in range(3):
            add_steps(user2, MONDAY - timedelta(days=14) + timedelta(days=i), 12000)
        self.assertIsNone(services.propose_step_goal(user2, MONDAY))

    def test_scheduler_creates_once_and_never_nags_within_28_days(self):
        add_weeks(self.user, 12000)
        self.assertEqual(services.create_step_goal_suggestions(NOW), 1)
        self.assertEqual(services.create_step_goal_suggestions(NOW), 0)
        StepGoalSuggestion.objects.update(created_at=NOW - timedelta(days=10))
        self.assertEqual(services.create_step_goal_suggestions(NOW + timedelta(days=7)), 0)


class StepGoalApiTests(APITestCase):
    def setUp(self):
        self.user = make_user("goalapi@example.test")
        self.other = make_user("goalapi2@example.test")
        self.s = StepGoalSuggestion.objects.create(user=self.user, current_goal=10000, suggested_goal=11000,
                                                   reason="r", week_start=MONDAY)
        self.client.force_authenticate(self.user)

    def test_accept_applies_goal_once_and_dismiss_does_not(self):
        self.assertEqual(self.client.get("/api/v1/automations/step-goal/").data["suggestion"]["suggested_goal"], 11000)
        res = self.client.post(f"/api/v1/automations/step-goal/{self.s.pk}/accept/")
        self.assertEqual(res.data["daily_step_goal"], 11000)
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.daily_step_goal, 11000)
        self.assertEqual(self.client.post(f"/api/v1/automations/step-goal/{self.s.pk}/accept/").status_code, 404)

    def test_dismiss_keeps_goal_and_other_users_cannot_answer(self):
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.post(f"/api/v1/automations/step-goal/{self.s.pk}/accept/").status_code, 404)
        self.client.force_authenticate(self.user)
        self.client.post(f"/api/v1/automations/step-goal/{self.s.pk}/dismiss/")
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.daily_step_goal, 10000)
        self.assertIsNone(self.client.get("/api/v1/automations/step-goal/").data["suggestion"])


class ReminderTimingTests(TestCase):
    def test_median_first_log_minus_fifteen_and_needs_seven_days(self):
        user = make_user("timing@example.test")
        for i in range(6):
            HydrationLog.objects.create(user=user, client_request_id=uuid.uuid4(), volume_ml=250,
                                        local_date=MONDAY - timedelta(days=i), time_zone="UTC")
        self.assertIsNone(services.suggest_water_reminder_start(user, NOW))
        for i in range(7):
            log = HydrationLog.objects.create(user=user, client_request_id=uuid.uuid4(), volume_ml=250,
                                              local_date=MONDAY - timedelta(days=i), time_zone="UTC")
            HydrationLog.objects.filter(pk=log.pk).update(created_at=NOW - timedelta(days=i, hours=1))  # 08:00 UTC
        result = services.suggest_water_reminder_start(user, NOW)
        self.assertEqual(result["suggested_start_minute"], 465)  # 07:45
        self.assertGreaterEqual(result["based_on_days"], 7)


class ReengagementTests(TestCase):
    def setUp(self):
        self.user = make_user("quiet@example.test")
        enable(self.user, reengagement_enabled=True)
        User.objects.filter(pk=self.user.pk).update(date_joined=NOW - timedelta(days=30))

    def test_sends_after_three_quiet_days_at_most_weekly(self):
        self.assertEqual(services.send_reengagement(NOW), 1)
        self.assertEqual(services.send_reengagement(NOW + timedelta(days=1)), 0)  # within a week
        self.assertEqual(services.send_reengagement(NOW + timedelta(days=7)), 1)

    def test_recent_activity_or_new_account_is_left_alone(self):
        HydrationLog.objects.create(user=self.user, client_request_id=uuid.uuid4(), volume_ml=250, local_date=MONDAY, time_zone="UTC")
        HydrationLog.objects.update(created_at=NOW - timedelta(days=1))
        self.assertEqual(services.send_reengagement(NOW), 0)
        user2 = make_user("new@example.test")
        enable(user2, reengagement_enabled=True)
        self.assertEqual(services.send_reengagement(NOW), 0)  # joined just now


class ChallengeSuggestionTests(TestCase):
    def test_suggests_reachable_unjoined_challenges_only(self):
        user = make_user("chal@example.test")
        for i in range(10):
            add_steps(user, MONDAY - timedelta(days=i), 8000)
        mk = lambda slug, target: Challenge.objects.create(
            slug=slug, title=slug, start_date=MONDAY - timedelta(days=1), end_date=MONDAY + timedelta(days=10),
            target_steps=target, reward_points=10, is_published=True)
        mk("easy", 60000)      # 6,000/day needed -> comfortable
        mk("stretch", 100000)  # 10,000/day -> stretch (1.25x)
        mk("far", 200000)      # 20,000/day -> out of reach
        got = services.suggest_challenges(user, MONDAY)
        self.assertEqual([(g["title"], g["fit"]) for g in got], [("easy", "comfortable"), ("stretch", "stretch")])
        self.assertEqual(services.suggest_challenges(make_user("nodata@example.test"), MONDAY), [])


class HrDigestTests(APITestCase):
    def _staff(self, n, dept, active):
        d, _ = Department.objects.get_or_create(name=dept)
        for i in range(n):
            u = make_user(f"{dept}{i}{uuid.uuid4().hex[:4]}@example.test")
            EmployeeProfile.objects.filter(user=u).update(department=d)
            if i < active:
                HydrationLog.objects.create(user=u, client_request_id=uuid.uuid4(), volume_ml=250, local_date=MONDAY, time_zone="UTC")

    def test_too_few_people_shows_no_figures(self):
        self._staff(4, "Tiny", 2)
        self.assertFalse(services.hr_digest(NOW)["available"])

    def test_small_departments_are_merged_and_hidden_unless_large_enough(self):
        self._staff(6, "Ops", 3)
        self._staff(2, "A", 1)
        self._staff(2, "B", 1)
        data = services.hr_digest(NOW)
        names = [d["name"] for d in data["departments"]]
        self.assertEqual(names, ["Ops"])  # merged A+B has 4 < 5, so not shown at all
        self.assertEqual(data["total_employees"], 10)
        self.assertEqual(data["active_employees"], 5)
        self._staff(2, "C", 0)
        self.assertIn("Other departments", [d["name"] for d in services.hr_digest(NOW)["departments"]])

    def test_endpoint_is_admin_only_and_digest_sent_to_admins_on_monday(self):
        self._staff(6, "Ops", 3)
        hr = make_user("hr@example.test", Role.HR_ADMIN)
        emp = User.objects.filter(role=Role.EMPLOYEE).first()
        self.client.force_authenticate(emp)
        self.assertEqual(self.client.get("/api/v1/automations/hr-digest/").status_code, 403)
        self.client.force_authenticate(hr)
        self.assertTrue(self.client.get("/api/v1/automations/hr-digest/").data["available"])
        self.assertEqual(services.send_hr_digests(NOW), 1)
        self.assertEqual(services.send_hr_digests(NOW), 0)
        self.assertEqual(Notification.objects.get(user=hr).kind, "hr_digest")
        enable(hr, hr_digest_enabled=False)
        self.assertEqual(services.send_hr_digests(NOW + timedelta(days=7)), 0)


class SchedulerTests(TestCase):
    def test_one_failing_automation_does_not_block_the_rest(self):
        with patch("automations.services.send_daily_coach", side_effect=RuntimeError("boom")):
            result = run_scheduled_automations()
        self.assertEqual(result["daily_coach"], "error")
        self.assertIn("weekly_review", result)
        self.assertNotEqual(result["weekly_review"], "error")
