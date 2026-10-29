"""Each challenge counts exactly its own dates (whole days), whenever someone joins."""
import uuid
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from challenges.models import Challenge, ChallengeEnrollment, ChallengeProgress, PointsLedger
from challenges.services import reconcile_user
from fitness.models import DailyActivity, FitnessAccount, FitnessSource


class ChallengeDatesTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("j@example.test", "Strong-passphrase-123")
        self.source = FitnessSource.objects.create(user=self.user, installation_id=uuid.uuid4())
        FitnessAccount.objects.create(user=self.user, active_source=self.source, time_zone="UTC")
        self.today = timezone.now().date()
        self.api = APIClient()
        self.api.force_authenticate(self.user)

    def sync(self, steps, day=None):
        DailyActivity.objects.update_or_create(
            user=self.user, source=self.source, local_date=day or self.today,
            defaults={"time_zone": "UTC", "observed_at": timezone.now(), "steps": steps,
                      "metric_status": {"steps": "available"}})
        reconcile_user(self.user)

    def challenge(self, **extra):
        values = dict(slug="c-" + uuid.uuid4().hex[:6], title="12k today", kind="steps", target_steps=12000,
                      start_date=self.today, end_date=self.today, reward_points=20, is_published=True)
        values.update(extra)
        return Challenge.objects.create(**values)

    def progress(self, challenge):
        return ChallengeProgress.objects.get(enrollment__challenge=challenge)

    def test_challenge_starting_today_counts_all_of_todays_steps(self):
        c = self.challenge()
        self.sync(5000)                                   # walked before joining: counts, it is today
        self.assertEqual(self.api.post(f"/api/v1/challenges/{c.pk}/join/").status_code, 201)
        self.assertEqual(self.progress(c).progress_value, 5000)
        self.sync(12400)
        p = self.progress(c)
        self.assertEqual(p.progress_value, 12400)
        self.assertTrue(p.is_complete)
        self.assertEqual(sum(PointsLedger.objects.filter(user=self.user).values_list("delta", flat=True)), 20)
        row = self.api.get("/api/v1/challenges/me/").json()["challenges"][0]
        self.assertEqual(row["progress_label"], "12,400 of 12,000 steps")
        self.assertIn("(the whole day) counts, even if you join later", row["counting_note"])

    def test_challenge_starting_tomorrow_counts_only_from_tomorrow(self):
        tomorrow = self.today + timedelta(days=1)
        c = self.challenge(start_date=tomorrow, end_date=tomorrow + timedelta(days=2))
        listed = self.api.get("/api/v1/challenges/").json()[0]
        self.assertTrue(listed["can_join"])               # can join before it starts
        self.assertTrue(listed["upcoming"])
        self.assertEqual(self.api.post(f"/api/v1/challenges/{c.pk}/join/").status_code, 201)
        self.sync(20000)                                  # today: before the start date, never counts
        self.assertEqual(self.progress(c).progress_value, 0)
        self.sync(7000, day=tomorrow)
        self.assertEqual(self.progress(c).progress_value, 7000)

    def test_steps_after_the_end_date_never_count_and_ended_challenges_cannot_be_joined(self):
        yesterday = self.today - timedelta(days=1)
        c = self.challenge(start_date=yesterday - timedelta(days=1), end_date=yesterday)
        ChallengeEnrollment.objects.create(challenge=c, user=self.user, joined_local_date=yesterday)
        self.sync(9000, day=yesterday)
        self.sync(9000)                                   # today is after the end date
        self.assertEqual(self.progress(c).progress_value, 9000)
        other = self.challenge(start_date=yesterday - timedelta(days=3), end_date=yesterday)
        response = self.api.post(f"/api/v1/challenges/{other.pk}/join/")
        self.assertEqual(response.status_code, 400)
        self.assertIn("ended", str(response.json()))

    def test_step_days_count_the_start_date_in_full(self):
        c = self.challenge(kind="step_days", target_steps=0, target_value=1, daily_threshold=8000,
                           end_date=self.today + timedelta(days=1))
        self.sync(9000)
        self.api.post(f"/api/v1/challenges/{c.pk}/join/")
        p = self.progress(c)
        self.assertEqual(p.progress_value, 1)
        self.assertTrue(p.is_complete)

    def test_one_day_challenges_of_any_type_can_be_created_in_the_portal(self):
        staff = User.objects.create_user("s@example.test", "Strong-passphrase-123", role="admin", is_staff=True)
        self.client.force_login(staff)
        for slug, kind, extra in (("today-12k", "steps", {"target_steps": 12000}),
                                  ("sleep-1", "sleep_nights", {"target_value": 1, "daily_threshold": 420})):
            data = {"slug": slug, "title": slug, "description": "", "kind": kind, "start_date": self.today,
                    "end_date": self.today, "target_steps": "", "target_running_minutes": "", "target_value": "",
                    "daily_threshold": "", "activity_type": "", "reward_points": 10, "badge_title": "",
                    "is_published": "on"}
            data.update(extra)
            self.assertEqual(self.client.post("/portal/challenges/new/", data).status_code, 302, slug)


class PasswordLikeEmailTests(TestCase):
    def test_password_may_be_similar_to_the_email(self):
        from django.contrib.auth.password_validation import validate_password
        user = User(email="asha.menon@infomeuae.com")
        validate_password("asha.menon@infomeuae.com", user=user)   # allowed by company choice
