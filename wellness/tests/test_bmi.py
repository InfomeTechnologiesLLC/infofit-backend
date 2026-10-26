from datetime import date, datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from rest_framework.test import APITestCase

from accounts.models import User
from notifications.models import Notification
from wellness.models import BodyMeasurement, ReminderSettings
from wellness.tasks import create_weekly_bmi_reminders

URL = "/api/v1/wellness/bmi/"


class BmiApiTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user("bmi@example.test", "Strong-passphrase-123")
        self.other = User.objects.create_user("other-bmi@example.test", "Strong-passphrase-123")
        self.client.force_authenticate(self.user)

    def test_empty_state(self):
        data = self.client.get(URL).data
        self.assertIsNone(data["latest"])
        self.assertEqual(data["weekly"], [])
        self.assertTrue(data["update_due"])

    def test_server_computes_bmi_and_same_day_post_updates(self):
        first = self.client.post(URL, {"height_cm": "170", "weight_kg": "70"}, format="json")
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(first.data["latest"]["bmi"], 24.2)
        self.assertEqual(first.data["latest"]["category"], "healthy_range")
        again = self.client.post(URL, {"height_cm": "170", "weight_kg": "72"}, format="json")
        self.assertEqual(again.status_code, 200)
        self.assertEqual(BodyMeasurement.objects.count(), 1)
        self.assertEqual(again.data["latest"]["weight_kg"], 72.0)
        self.assertFalse(again.data["update_due"])

    def test_validation_rejects_implausible_values(self):
        for body in ({"height_cm": "50", "weight_kg": "70"}, {"height_cm": "170", "weight_kg": "5"},
                     {"height_cm": "170"}, {"height_cm": "abc", "weight_kg": "70"}):
            self.assertEqual(self.client.post(URL, body, format="json").status_code, 400, body)

    def test_weekly_series_changes_and_total(self):
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        for offset, kg in ((-14, "80.0"), (-7, "79.0"), (-6, "78.5"), (0, "77.0")):
            BodyMeasurement.objects.create(
                user=self.user, height_cm="175.0", weight_kg=kg, bmi="25.0",
                local_date=monday + timedelta(days=offset), time_zone="UTC")
        data = self.client.get(URL).data
        self.assertEqual([w["weight_kg"] for w in data["weekly"]], [80.0, 78.5, 77.0])  # last entry per week
        self.assertEqual(data["weekly"][0]["weight_change_kg"], None)
        self.assertEqual(data["weekly"][1]["weight_change_kg"], -1.5)
        self.assertEqual(data["total_weight_change_kg"], -3.0)

    def test_isolation_and_delete(self):
        self.client.post(URL, {"height_cm": "170", "weight_kg": "70"}, format="json")
        entry = BodyMeasurement.objects.get()
        self.client.force_authenticate(self.other)
        self.assertIsNone(self.client.get(URL).data["latest"])
        self.assertEqual(self.client.delete(f"{URL}{entry.pk}/").status_code, 404)
        self.client.force_authenticate(self.user)
        self.assertIsNone(self.client.delete(f"{URL}{entry.pk}/").data["latest"])

    def test_requires_authentication(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(URL).status_code, 401)


class BmiReminderTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user("bmi-rem@example.test", "Strong-passphrase-123")
        ReminderSettings.objects.create(user=self.user, bmi_enabled=True, time_zone="UTC", start_minute=540)

    def run_at(self, hour, minute=0, day=date(2026, 10, 26)):
        now = datetime(day.year, day.month, day.day, hour, minute, tzinfo=dt_timezone.utc)
        with patch("wellness.tasks.timezone.now", return_value=now):
            return create_weekly_bmi_reminders()

    def test_sends_once_per_week_only_when_due(self):
        self.assertEqual(self.run_at(9, 0), 1)
        self.assertEqual(self.run_at(9, 2), 0)  # same ISO week: deduplicated
        self.assertEqual(Notification.objects.filter(kind="bmi_checkin").count(), 1)

    def test_skips_outside_slot_when_recent_or_disabled(self):
        self.assertEqual(self.run_at(14, 0), 0)
        BodyMeasurement.objects.create(user=self.user, height_cm="170", weight_kg="70", bmi="24.2",
                                       local_date=date(2026, 10, 22), time_zone="UTC")
        self.assertEqual(self.run_at(9, 0), 0)  # measured 4 days ago
        ReminderSettings.objects.update(bmi_enabled=False)
        self.assertEqual(self.run_at(9, 0, day=date(2026, 11, 9)), 0)
