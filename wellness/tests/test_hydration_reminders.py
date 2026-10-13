import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient, APITestCase

from accounts.models import User
from notifications.models import Notification, PushDelivery, PushDevice
from wellness.models import HydrationLog, ReminderSettings
from wellness.tasks import create_wellness_reminders


class HydrationApiTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user("water@example.test", "Strong-passphrase-123")
        self.other = User.objects.create_user("other-water@example.test", "Strong-passphrase-123")
        self.client.force_authenticate(self.user)

    def test_log_replay_undo_and_employee_isolation(self):
        request_id = str(uuid.uuid4())
        url = "/api/v1/wellness/hydration/logs/"
        first = self.client.post(url, {"client_request_id": request_id, "volume_ml": 250}, format="json")
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(first.data["total_ml"], 250)
        self.assertTrue(self.client.get("/api/v1/wellness/hydration/today/", {
            "client_request_id": request_id
        }).data["request_seen"])
        self.assertEqual(self.client.post(url, {"client_request_id": request_id, "volume_ml": 250},
                                          format="json").status_code, 200)
        self.assertEqual(HydrationLog.objects.count(), 1)
        self.assertEqual(self.client.post(url, {"client_request_id": request_id, "volume_ml": 500},
                                          format="json").status_code, 409)
        self.assertEqual(self.client.post(url, {"client_request_id": str(uuid.uuid4()), "volume_ml": 0},
                                          format="json").status_code, 400)
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get("/api/v1/wellness/hydration/today/").data["total_ml"], 0)
        self.assertEqual(self.client.delete(f"{url}{first.data['log_id']}/").status_code, 404)
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.delete(f"{url}{first.data['log_id']}/").data["total_ml"], 0)
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get("/api/v1/wellness/hydration/today/").status_code, 401)

    def test_reminder_settings_are_opt_in_and_scoped(self):
        url = "/api/v1/wellness/reminders/"
        initial = self.client.get(url)
        self.assertEqual(initial.status_code, 200)
        self.assertFalse(initial.data["water_enabled"])
        self.assertFalse(initial.data["movement_enabled"])
        updated = self.client.patch(url, {"water_enabled": True, "movement_enabled": True,
                                          "time_zone": "Asia/Kolkata"}, format="json")
        self.assertEqual(updated.status_code, 200, updated.data)
        self.assertEqual(updated.data["time_zone"], "Asia/Kolkata")
        self.assertEqual(self.client.patch(url, {"time_zone": "invalid/zone"}, format="json").status_code, 400)
        self.assertEqual(self.client.patch(url, {"start_hour": 20, "end_hour": 9}, format="json").status_code, 400)
        self.client.force_authenticate(self.other)
        self.assertFalse(self.client.get(url).data["water_enabled"])
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get(url).status_code, 401)


class ReminderTimeSettingsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("times@example.test", "Strong-passphrase-123")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.url = "/api/v1/wellness/reminders/"

    def test_minute_times_and_challenge_toggle_are_saved(self):
        response = self.client.patch(self.url, {
            "start_minute": 8 * 60 + 30, "end_minute": 17 * 60 + 45, "challenge_enabled": True,
            "challenge_minute": 18 * 60 + 15, "water_interval_minutes": 45, "time_zone": "Asia/Kolkata",
        }, format="json")
        self.assertEqual(response.status_code, 200)
        data = self.client.get(self.url).data
        self.assertEqual((data["start_minute"], data["end_minute"]), (510, 1065))
        self.assertEqual((data["start_hour"], data["end_hour"]), (8, 18))
        self.assertTrue(data["challenge_enabled"])
        self.assertEqual(data["challenge_minute"], 1095)
        self.assertEqual(data["water_interval_minutes"], 45)

    def test_legacy_hours_still_work_and_bad_windows_are_rejected(self):
        self.assertEqual(self.client.patch(self.url, {"start_hour": 7, "end_hour": 21}, format="json").status_code, 200)
        data = self.client.get(self.url).data
        self.assertEqual((data["start_minute"], data["end_minute"]), (420, 1260))
        self.assertEqual(self.client.patch(self.url, {"start_minute": 600, "end_minute": 600}, format="json").status_code, 400)
        self.assertEqual(self.client.patch(self.url, {"water_interval_minutes": 10}, format="json").status_code, 400)
        self.assertEqual(self.client.patch(self.url, {"end_minute": 1440}, format="json").status_code, 200)


class ReminderTaskTests(TestCase):
    def test_opt_in_daytime_reminders_are_idempotent_and_do_not_claim_sitting(self):
        user = User.objects.create_user("remind@example.test", "Strong-passphrase-123")
        ReminderSettings.objects.create(user=user, water_enabled=True, movement_enabled=True,
                                        time_zone="UTC", start_hour=9, end_hour=19)
        at_noon = datetime(2026, 10, 7, 12, 0, tzinfo=dt_timezone.utc)
        with patch("wellness.tasks.timezone.now", return_value=at_noon):
            self.assertEqual(create_wellness_reminders(), 2)
            self.assertEqual(create_wellness_reminders(), 0)
        self.assertEqual(Notification.objects.filter(user=user).count(), 2)
        self.assertFalse(any("sitting" in row.body.lower() for row in Notification.objects.filter(user=user)))
        with patch("wellness.tasks.timezone.now", return_value=at_noon.replace(hour=20)):
            self.assertEqual(create_wellness_reminders(), 0)

    def test_custom_start_minute_and_interval(self):
        user = User.objects.create_user("custom@example.test", "Strong-passphrase-123")
        ReminderSettings.objects.create(user=user, water_enabled=True, time_zone="UTC",
                                        start_minute=9 * 60 + 15, end_minute=12 * 60, water_interval_minutes=45)
        PushDevice.objects.create(user=user, token="device-token-for-test")
        base = datetime(2026, 10, 7, 9, 15, tzinfo=dt_timezone.utc)
        with patch("wellness.tasks.timezone.now", return_value=base):
            self.assertEqual(create_wellness_reminders(), 1)  # first one at the start time
        with patch("wellness.tasks.timezone.now", return_value=base + timedelta(minutes=20)):
            self.assertEqual(create_wellness_reminders(), 0)
        with patch("wellness.tasks.timezone.now", return_value=base + timedelta(minutes=46)):
            self.assertEqual(create_wellness_reminders(), 1)  # 10:00
        self.assertFalse(PushDelivery.objects.exists())  # the phone shows reminders itself

    def test_recent_water_log_suppresses_water_prompt_not_movement_prompt(self):
        user = User.objects.create_user("logged@example.test", "Strong-passphrase-123")
        ReminderSettings.objects.create(user=user, water_enabled=True, movement_enabled=True, time_zone="UTC")
        at_noon = datetime(2026, 10, 7, 12, 0, tzinfo=dt_timezone.utc)
        log = HydrationLog.objects.create(user=user, client_request_id=uuid.uuid4(), volume_ml=250,
                                          local_date=at_noon.date(), time_zone="UTC")
        HydrationLog.objects.filter(pk=log.pk).update(created_at=at_noon - timedelta(minutes=10))
        with patch("wellness.tasks.timezone.now", return_value=at_noon):
            self.assertEqual(create_wellness_reminders(), 1)
        self.assertEqual(Notification.objects.get(user=user).kind, Notification.Kind.MOVEMENT_BREAK)
