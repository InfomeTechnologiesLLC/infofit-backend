from datetime import date, datetime, time, timedelta
from unittest.mock import patch
from uuid import uuid4

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import EmployeeProfile, Team, User
from challenges.models import BadgeAward, Challenge, ChallengeEnrollment, PointsLedger
from fitness.models import ExerciseRecord, FitnessAccount
from challenges.services import reconcile_user
from challenges.tasks import create_challenge_reminders
from wellness.models import ReminderSettings
from notifications.models import Notification


@override_settings(USE_TZ=True)
class ChallengeTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="runner@example.test", password="long-password")
        self.other = User.objects.create_user(email="other@example.test", password="long-password")
        self.client.force_authenticate(self.user)
        self.today = timezone.now().date()
        self.challenge = Challenge.objects.create(
            slug="test-walk", title="Test Walk", start_date=self.today,
            end_date=self.today, target_steps=100, reward_points=25,
            badge_title="Walker", is_published=True,
        )
        self.source = str(uuid4())
        self.client.put("/api/v1/fitness/source/", {
            "installation_id": self.source, "time_zone": "UTC", "device_label": "Test"
        }, format="json")

    def sync(self, steps, sequence, status="available"):
        day = {"local_date": self.today.isoformat(), "observed_at": "2026-10-01T12:00:00Z",
               "steps": steps if status == "available" else None, "distance_m": None,
               "active_kcal": None, "exercise_minutes": None, "sleep_minutes": None,
               "metric_status": {"steps": status, "distance_m": "no_data", "active_kcal": "no_data",
                                 "exercise_minutes": "no_data", "sleep_minutes": "no_data"},
               "exercises": [], "sleep": []}
        return self.client.post("/api/v1/fitness/sync/", {
            "installation_id": self.source, "idempotency_key": str(uuid4()),
            "sequence": sequence, "time_zone": "UTC", "days": [day]
        }, format="json")

    def test_join_sync_correction_and_replay(self):
        url = f"/api/v1/challenges/{self.challenge.id}/join/"
        self.assertEqual(self.client.post(url).status_code, 201)
        self.assertEqual(self.client.post(url).status_code, 200)
        self.assertEqual(ChallengeEnrollment.objects.count(), 1)
        # Simulate an enrollment from the previous local day to exercise full-day step scoring.
        ChallengeEnrollment.objects.update(joined_local_date=self.today - timedelta(days=1))
        self.challenge.end_date = date(2020, 1, 1)
        self.challenge.save()
        self.assertEqual(self.client.post(url).status_code, 200)
        self.challenge.end_date = self.today
        self.challenge.save()
        self.assertEqual(self.sync(120, 1).status_code, 200)
        self.assertEqual(self.client.get("/api/v1/challenges/me/").data["points_total"], 25)
        self.assertEqual(PointsLedger.objects.count(), 1)
        self.assertTrue(BadgeAward.objects.get().is_active)
        self.assertEqual(Notification.objects.filter(user=self.user).count(), 2)
        self.assertEqual(self.sync(120, 2).status_code, 200)
        self.assertEqual(PointsLedger.objects.count(), 1)
        self.assertEqual(Notification.objects.filter(user=self.user).count(), 2)
        self.assertEqual(self.sync(80, 3).status_code, 200)
        self.assertEqual(self.client.get("/api/v1/challenges/me/").data["points_total"], 0)
        self.assertEqual(list(PointsLedger.objects.values_list("delta", flat=True)), [25, -25])
        self.assertFalse(BadgeAward.objects.get().is_active)
        self.assertEqual(self.sync(120, 4).status_code, 200)
        self.assertEqual(self.client.get("/api/v1/challenges/me/").data["points_total"], 25)
        self.assertTrue(BadgeAward.objects.get().is_active)
        self.assertEqual(Notification.objects.filter(user=self.user).count(), 4)

    def test_unavailable_steps_never_score_and_switch_reverses(self):
        self.client.post(f"/api/v1/challenges/{self.challenge.id}/join/")
        ChallengeEnrollment.objects.update(joined_local_date=self.today - timedelta(days=1))
        self.assertEqual(self.sync(None, 1, "permission_unavailable").status_code, 200)
        self.assertIsNone(self.client.get("/api/v1/challenges/me/").data["challenges"][0]["verified_steps"])
        self.assertEqual(PointsLedger.objects.count(), 0)
        self.sync(120, 2)
        new_source = str(uuid4())
        response = self.client.put("/api/v1/fitness/source/", {
            "installation_id": new_source, "time_zone": "UTC", "replace_existing": True
        }, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/v1/challenges/me/").data["points_total"], 0)
        self.assertEqual(PointsLedger.objects.count(), 2)

    def test_auth_privacy_and_team_totals(self):
        team = Team.objects.create(name="Test Team")
        EmployeeProfile.objects.filter(user=self.user).update(team=team)
        self.client.post(f"/api/v1/challenges/{self.challenge.id}/join/")
        ChallengeEnrollment.objects.update(joined_local_date=self.today - timedelta(days=1))
        self.sync(120, 1)
        individual = self.client.get("/api/v1/challenges/leaderboard/?scope=individual")
        self.assertEqual(individual.status_code, 200)
        self.assertNotIn("steps", individual.data[0])
        teams = self.client.get("/api/v1/challenges/leaderboard/?scope=team")
        self.assertEqual(teams.data[0]["points"], 25)
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get("/api/v1/challenges/me/").data["points_total"], 0)
        self.assertEqual(self.client.get("/api/v1/challenges/leaderboard/").data[0]["name"], "Participant")
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get("/api/v1/challenges/").status_code, 401)

    def test_join_requires_published_current_challenge(self):
        self.challenge.is_published = False
        self.challenge.save()
        self.assertEqual(self.client.post(f"/api/v1/challenges/{self.challenge.id}/join/").status_code, 404)
        self.challenge.is_published = True
        self.challenge.end_date = date(2020, 1, 1)
        self.challenge.save()
        self.assertEqual(self.client.post(f"/api/v1/challenges/{self.challenge.id}/join/").status_code, 400)

    def test_join_day_steps_do_not_award_points(self):
        self.assertEqual(self.sync(2000, 1).status_code, 200)
        self.assertEqual(self.client.post(f"/api/v1/challenges/{self.challenge.pk}/join/").status_code, 201)
        self.assertEqual(self.client.get("/api/v1/challenges/me/").data["points_total"], 0)
        self.assertEqual(PointsLedger.objects.count(), 0)

    def test_running_requires_post_join_running_session(self):
        self.challenge.kind = Challenge.Kind.RUNNING
        self.challenge.target_running_minutes = 20
        self.challenge.save()
        self.sync(2000, 1)
        self.client.post(f"/api/v1/challenges/{self.challenge.pk}/join/")
        enrollment = ChallengeEnrollment.objects.get()
        source = FitnessAccount.objects.get(user=self.user).active_source
        now = timezone.now()
        ExerciseRecord.objects.create(user=self.user, source=source, record_id="old-run",
            origin_package="test", local_date=self.today, start_at=now - timedelta(hours=2),
            end_at=now - timedelta(minutes=80), exercise_type="56")
        reconcile_user(self.user)
        self.assertEqual(PointsLedger.objects.count(), 0)
        ExerciseRecord.objects.create(user=self.user, source=source, record_id="walk",
            origin_package="test", local_date=self.today, start_at=now + timedelta(minutes=1),
            end_at=now + timedelta(minutes=41), exercise_type="53")
        reconcile_user(self.user)
        self.assertEqual(PointsLedger.objects.count(), 0)
        ExerciseRecord.objects.create(user=self.user, source=source, record_id="new-run",
            origin_package="test", local_date=self.today, start_at=now + timedelta(minutes=1),
            end_at=now + timedelta(minutes=26), exercise_type="56")
        reconcile_user(self.user)
        self.assertEqual(self.client.get("/api/v1/challenges/me/").data["points_total"], 25)
        self.assertEqual(self.client.get("/api/v1/challenges/me/").data["challenges"][0]["verified_running_minutes"], 25)

    def test_daily_iva_reminder_is_once_per_day_and_stops_after_completion(self):
        self.client.post(f"/api/v1/challenges/{self.challenge.pk}/join/")
        at_four = timezone.make_aware(datetime.combine(self.today, time(16)))
        with patch("challenges.tasks.timezone.now", return_value=at_four):
            # Challenge reminders are opt-in.
            self.assertEqual(create_challenge_reminders(), 0)
            ReminderSettings.objects.create(user=self.user, challenge_enabled=True, challenge_minute=16 * 60)
            self.assertEqual(create_challenge_reminders(), 1)
            self.assertEqual(create_challenge_reminders(), 0)
            self.assertEqual(Notification.objects.filter(kind=Notification.Kind.CHALLENGE_REMINDER).count(), 1)
            self.challenge.end_date = self.today + timedelta(days=1)
            self.challenge.save()
            enrollment = ChallengeEnrollment.objects.get()
            enrollment.progress.is_complete = True
            enrollment.progress.save(update_fields=["is_complete"])
        with patch("challenges.tasks.timezone.now", return_value=at_four + timedelta(days=1)):
            self.assertEqual(create_challenge_reminders(), 0)

    def test_challenge_reminder_waits_for_chosen_time(self):
        self.client.post(f"/api/v1/challenges/{self.challenge.pk}/join/")
        ReminderSettings.objects.create(user=self.user, challenge_enabled=True, challenge_minute=18 * 60 + 30)
        at_six = timezone.make_aware(datetime.combine(self.today, time(18)))
        with patch("challenges.tasks.timezone.now", return_value=at_six):
            self.assertEqual(create_challenge_reminders(), 0)
        with patch("challenges.tasks.timezone.now", return_value=at_six + timedelta(minutes=35)):
            self.assertEqual(create_challenge_reminders(), 1)

    def test_admin_login_page_renders_for_signed_out_visitor(self):
        self.client.force_authenticate(user=None)
        response = self.client.get("/admin/login/")
        self.assertEqual(response.status_code, 200)
