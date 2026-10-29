"""Every challenge kind scores only automatically recorded data from the active phone."""
import uuid
from datetime import date, datetime, timedelta, timezone as dt_timezone

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from challenges.models import Challenge, ChallengeEnrollment, ChallengeProgress, PointsLedger
from challenges.services import reconcile_user
from fitness.models import DailyActivity, ExerciseRecord, FitnessAccount, FitnessSource, SleepRecord

START = date(2026, 10, 1)
END = date(2026, 10, 31)
JOINED = datetime(2026, 10, 1, 9, 0, tzinfo=dt_timezone.utc)


def at(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=dt_timezone.utc)


class ScoringTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="sam@example.test", password="x")
        self.source = FitnessSource.objects.create(user=self.user, installation_id=uuid.uuid4())
        FitnessAccount.objects.create(user=self.user, active_source=self.source, time_zone="UTC")
        self.other_source = FitnessSource.objects.create(user=self.user, installation_id=uuid.uuid4())

    def challenge(self, kind, target, **extra):
        fields = {"steps": {"target_steps": target}, "running": {"target_running_minutes": target}}.get(
            kind, {"target_value": target})
        c = Challenge.objects.create(slug=f"c-{uuid.uuid4().hex[:8]}", title=f"{kind} test", kind=kind,
                                     start_date=START, end_date=END, reward_points=50, is_published=True,
                                     **fields, **extra)
        enrollment = ChallengeEnrollment.objects.create(challenge=c, user=self.user, joined_local_date=START)
        ChallengeEnrollment.objects.filter(pk=enrollment.pk).update(joined_at=JOINED)
        return c

    def day(self, d, source=None, **values):
        status = {m: ("available" if values.get(m) is not None else "no_data")
                  for m in ("steps", "distance_m", "active_kcal", "exercise_minutes", "sleep_minutes")}
        DailyActivity.objects.create(user=self.user, source=source or self.source, local_date=date(2026, 10, d),
                                     time_zone="UTC", observed_at=timezone.now(), metric_status=status, **values)

    def workout(self, d, start, minutes, kind="79", method="automatic", source=None):
        begin = at(d, *start)
        ExerciseRecord.objects.create(user=self.user, source=source or self.source, record_id=uuid.uuid4().hex,
                                      origin_package="com.example.watch", local_date=date(2026, 10, d),
                                      start_at=begin, end_at=begin + timedelta(minutes=minutes),
                                      exercise_type=kind, recording_method=method)

    def progress(self, challenge):
        reconcile_user(self.user)
        return ChallengeProgress.objects.get(enrollment__challenge=challenge)

    def points(self):
        return sum(PointsLedger.objects.filter(user=self.user).values_list("delta", flat=True))

    # ---- daily totals ----

    def test_steps_typed_in_by_hand_do_not_count(self):
        c = self.challenge("steps", 20000)
        self.day(2, steps=12000, manual_steps=5000)
        self.day(3, steps=9000)
        p = self.progress(c)
        self.assertEqual(p.progress_value, 16000)
        self.assertFalse(p.is_complete)
        self.assertEqual(self.points(), 0)

    def test_joining_day_and_other_phones_do_not_count(self):
        c = self.challenge("steps", 1000)
        self.day(1, steps=30000)                       # the joining day
        self.day(2, steps=30000, source=self.other_source)  # a second phone
        p = self.progress(c)
        self.assertEqual(p.progress_value, 0)
        self.assertFalse(p.is_complete)

    def test_impossible_days_are_capped_and_flagged(self):
        c = self.challenge("steps", 200000)
        self.day(2, steps=90000)
        self.day(3, steps=10000)
        p = self.progress(c)
        self.assertEqual(p.progress_value, 60000)
        self.assertEqual(p.flagged_days, 1)

    def test_distance_in_km_minus_manual(self):
        c = self.challenge("distance", 10)
        self.day(2, distance_m=6400.0, manual_distance_m=1000.0)
        self.day(3, distance_m=5000.0)
        p = self.progress(c)
        self.assertEqual(p.progress_value, 10.4)
        self.assertTrue(p.is_complete)
        self.assertEqual(self.points(), 50)

    def test_calories(self):
        c = self.challenge("calories", 1000)
        self.day(2, active_kcal=700.0, manual_active_kcal=300.0)
        self.day(3, active_kcal=400.0)
        p = self.progress(c)
        self.assertEqual(p.progress_value, 800)
        self.assertFalse(p.is_complete)

    def test_step_days_count_days_over_the_daily_goal(self):
        c = self.challenge("step_days", 2, daily_threshold=8000)
        self.day(2, steps=9000)
        self.day(3, steps=12000, manual_steps=5000)   # only 7,000 recorded automatically
        self.day(4, steps=8000)
        p = self.progress(c)
        self.assertEqual(p.progress_value, 2)
        self.assertTrue(p.is_complete)

    # ---- workouts ----

    def test_active_minutes_merge_overlaps_and_ignore_manual_and_short(self):
        c = self.challenge("active_minutes", 60)
        self.workout(2, (7, 0), 30)                     # watch
        self.workout(2, (7, 10), 30, kind="56")         # phone app, overlapping: adds 10 minutes
        self.workout(3, (7, 0), 120, method="manual")   # typed in: ignored
        self.workout(4, (7, 0), 5)                      # too short: ignored
        self.workout(1, (8, 0), 60)                     # before joining at 09:00: ignored
        p = self.progress(c)
        self.assertEqual(p.progress_value, 40)
        self.assertFalse(p.is_complete)

    def test_workout_counts_only_the_chosen_activity(self):
        c = self.challenge("workout", 45, activity_type="cycling")
        self.workout(2, (18, 0), 30, kind="8")    # biking
        self.workout(3, (18, 0), 20, kind="9")    # stationary bike
        self.workout(4, (18, 0), 60, kind="83")   # yoga: not cycling
        p = self.progress(c)
        self.assertEqual(p.progress_value, 50)
        self.assertTrue(p.is_complete)

    def test_one_session_counts_for_at_most_four_hours(self):
        c = self.challenge("running", 600)
        self.workout(2, (6, 0), 600, kind="56")
        self.assertEqual(self.progress(c).progress_value, 240)

    def test_sleep_nights(self):
        c = self.challenge("sleep_nights", 2, daily_threshold=420)
        for d, hours, method in ((2, 8, "automatic"), (3, 6, "automatic"), (4, 9, "manual"), (5, 7, "unknown")):
            begin = at(d, 0) - timedelta(hours=1)
            SleepRecord.objects.create(user=self.user, source=self.source, record_id=uuid.uuid4().hex,
                                       origin_package="com.example.watch", local_date=date(2026, 10, d),
                                       start_at=begin, end_at=begin + timedelta(hours=hours), recording_method=method)
        p = self.progress(c)
        self.assertEqual(p.progress_value, 2)   # 8 h and 7 h nights; 6 h too short, the manual 9 h ignored
        self.assertTrue(p.is_complete)

    def test_points_are_taken_back_when_data_is_corrected(self):
        c = self.challenge("distance", 5)
        self.day(2, distance_m=6000.0)
        self.assertTrue(self.progress(c).is_complete)
        DailyActivity.objects.filter(user=self.user).update(manual_distance_m=3000.0)
        p = self.progress(c)
        self.assertFalse(p.is_complete)
        self.assertEqual(self.points(), 0)


class ChallengeRulesTests(TestCase):
    def make(self, **fields):
        base = dict(slug="x", title="x", start_date=START, end_date=date(2026, 10, 10), reward_points=10)
        base.update(fields)
        return Challenge(**base)

    def test_new_kinds_need_their_settings(self):
        with self.assertRaises(ValidationError):
            self.make(kind="distance").full_clean()
        with self.assertRaises(ValidationError):
            self.make(kind="workout", target_value=100).full_clean()
        with self.assertRaises(ValidationError):
            self.make(kind="step_days", target_value=5, daily_threshold=100).full_clean()
        self.make(kind="step_days", target_value=5, daily_threshold=8000).full_clean()

    def test_day_count_target_cannot_exceed_the_days_available(self):
        with self.assertRaises(ValidationError):
            self.make(kind="sleep_nights", target_value=10, daily_threshold=420).full_clean()  # 9 countable nights
        self.make(kind="sleep_nights", target_value=9, daily_threshold=420).full_clean()

    def test_labels(self):
        self.assertEqual(self.make(kind="step_days", target_value=5, daily_threshold=8000).target_label,
                         "5 days with 8,000+ steps")
        self.assertEqual(self.make(kind="workout", target_value=300, activity_type="cycling").target_label,
                         "300 min of cycling")
        self.assertEqual(self.make(kind="sleep_nights", target_value=5, daily_threshold=450).target_label,
                         "5 nights with 7.5+ hours of sleep")
