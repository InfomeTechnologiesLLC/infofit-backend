from uuid import uuid4
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from fitness.models import DailyActivity, ExerciseRecord, SleepRecord


class FitnessApiTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="one@example.test", password="Strong-Passphrase-123")
        self.other = User.objects.create_user(email="two@example.test", password="Strong-Passphrase-123")
        self.client.force_authenticate(self.user)
        self.installation = str(uuid4())
        self.client.put(reverse("fitness_source"), {
            "installation_id": self.installation, "time_zone": "America/Los_Angeles",
            "device_label": "Pixel"
        }, format="json")

    def day(self, steps=100, exercise=True):
        return {
            "local_date": "2026-09-30", "observed_at": "2026-10-01T06:00:00Z",
            "steps": steps, "distance_m": None, "active_kcal": None,
            "exercise_minutes": 30, "sleep_minutes": 420,
            "metric_status": {"steps": "available", "distance_m": "no_data",
                              "active_kcal": "permission_unavailable", "exercise_minutes": "available",
                              "sleep_minutes": "available"},
            "exercises": [{"record_id": "ex1", "origin_package": "test.app",
                           "start_at": "2026-09-30T15:00:00Z", "end_at": "2026-09-30T15:30:00Z",
                           "exercise_type": "running"}] if exercise else [],
            "sleep": [{"record_id": "sl1", "origin_package": "test.app",
                       "start_at": "2026-09-30T05:00:00Z", "end_at": "2026-09-30T13:00:00Z"}],
        }

    def sync(self, day=None, sequence=1, key=None, installation=None):
        return self.client.post(reverse("fitness_sync"), {
            "installation_id": installation or self.installation,
            "idempotency_key": key or str(uuid4()), "sequence": sequence,
            "time_zone": "America/Los_Angeles", "days": [day or self.day()],
        }, format="json")

    def test_requires_authentication(self):
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get(reverse("fitness_home")).status_code, 401)
        self.assertEqual(self.sync().status_code, 401)

    def test_sync_and_home_are_isolated(self):
        self.assertEqual(self.sync().status_code, 200)
        response = self.client.get(reverse("fitness_home"), {"date": "2026-09-30"})
        self.assertEqual(response.data["metrics"]["steps"], 100)
        self.assertIsNone(response.data["metrics"]["active_kcal"])
        self.assertEqual(response.data["step_estimates"]["distance_m"], 75.0)
        self.assertEqual(response.data["step_estimates"]["active_kcal"], 4.0)
        self.assertEqual(ExerciseRecord.objects.count(), 1)
        self.assertEqual(SleepRecord.objects.count(), 1)
        self.client.force_authenticate(self.other)
        response = self.client.get(reverse("fitness_home"), {"date": "2026-09-30"})
        self.assertIsNone(response.data["metrics"]["steps"])
        self.assertEqual(self.client.get(reverse("fitness_exercises"), {"from": "2026-09-30", "to": "2026-09-30"}).data, [])

    def test_week_and_month_step_summary_excludes_missing_days(self):
        from fitness.models import FitnessAccount
        account = FitnessAccount.objects.get(user=self.user)
        today = timezone.now().astimezone(ZoneInfo(account.time_zone)).date()
        days = [today, today - timedelta(days=1)]
        for day, steps in zip(days, [1200, 800]):
            DailyActivity.objects.create(user=self.user, source=account.active_source,
                local_date=day, time_zone=account.time_zone, steps=steps,
                metric_status={"steps": "available"}, observed_at=timezone.now())
        response = self.client.get(reverse("fitness_step_summary"), {"period": "week"})
        self.assertEqual(response.status_code, 200)
        expected = 2000 if today.weekday() else 1200
        self.assertEqual(response.data["steps"], expected)
        self.assertEqual(response.data["days_with_data"], 2 if today.weekday() else 1)
        self.assertEqual(self.client.get(reverse("fitness_step_summary"), {"period": "year"}).status_code, 400)

    def test_retry_correction_and_deletion(self):
        key = str(uuid4())
        body = {"installation_id": self.installation, "idempotency_key": key,
                "sequence": 1, "time_zone": "America/Los_Angeles", "days": [self.day()]}
        self.assertEqual(self.client.post(reverse("fitness_sync"), body, format="json").status_code, 200)
        self.assertEqual(self.client.post(reverse("fitness_sync"), body, format="json").status_code, 200)
        self.assertEqual(DailyActivity.objects.count(), 1)
        body["days"][0]["steps"] = 999
        self.assertEqual(self.client.post(reverse("fitness_sync"), body, format="json").status_code, 409)
        self.assertEqual(self.sync(self.day(steps=80, exercise=False), sequence=2).status_code, 200)
        self.assertEqual(DailyActivity.objects.get().steps, 80)
        self.assertEqual(ExerciseRecord.objects.count(), 0)
        self.assertEqual(self.sync(sequence=1).status_code, 409)

    def test_source_switch_must_be_explicit(self):
        self.assertEqual(self.sync().status_code, 200)
        second = str(uuid4())
        body = {"installation_id": second, "time_zone": "UTC"}
        self.assertEqual(self.client.put(reverse("fitness_source"), body, format="json").status_code, 409)
        self.assertEqual(self.sync(installation=second).status_code, 409)
        body["replace_existing"] = True
        self.assertEqual(self.client.put(reverse("fitness_source"), body, format="json").status_code, 200)
        self.assertIsNone(self.client.get(reverse("fitness_home"), {"date": "2026-09-30"}).data["metrics"]["steps"])

    def test_revoked_permission_does_not_delete_existing_records(self):
        self.assertEqual(self.sync().status_code, 200)
        day = self.day(steps=120, exercise=False)
        day["exercise_minutes"] = None
        day["metric_status"]["exercise_minutes"] = "permission_unavailable"
        self.assertEqual(self.sync(day, sequence=2).status_code, 200)
        self.assertEqual(ExerciseRecord.objects.count(), 1)

    def test_rejects_false_availability_and_wrong_sleep_date(self):
        day = self.day()
        day["active_kcal"] = 0
        self.assertEqual(self.sync(day).status_code, 400)
        day = self.day()
        day["sleep"][0]["end_at"] = "2026-10-02T13:00:00Z"
        self.assertEqual(self.sync(day).status_code, 400)

    def test_omitted_null_metrics_are_accepted_from_android_json(self):
        day = self.day()
        day.pop("distance_m")
        day.pop("active_kcal")
        response = self.sync(day)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNone(DailyActivity.objects.get().distance_m)

    def test_account_deletion_clears_fitness_rows(self):
        self.assertEqual(self.sync().status_code, 200)
        self.user.delete()
        self.assertEqual(DailyActivity.objects.count(), 0)
        self.assertEqual(ExerciseRecord.objects.count(), 0)
