import io
import tempfile
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Role, User
from challenges.models import Challenge
from notifications.models import Notification, PushDelivery, PushDevice, SchedulerHeartbeat
from notifications.services import create_notification
from notifications.tasks import deliver_now, push_problem

PW = "Strong-passphrase-123"


class SchedulerCommandTests(TestCase):
    def test_command_runs_every_job_and_records_a_heartbeat(self):
        user = User.objects.create_user("e@example.test", PW)
        today = timezone.localdate()
        Challenge.objects.create(slug="c", title="C", start_date=today, end_date=today, target_steps=100,
                                 reward_points=1, is_published=True)
        out = io.StringIO()
        with override_settings(FIREBASE_CREDENTIALS_FILE=""):
            call_command("run_scheduled_jobs", stdout=out)
        self.assertIn("automations", out.getvalue())
        beat = SchedulerHeartbeat.objects.get(name="cron")
        self.assertTrue(beat.last_ok)
        self.assertTrue(Notification.objects.filter(user=user, kind="new_challenge").exists())
        push = SchedulerHeartbeat.objects.get(name="push_delivery")
        self.assertFalse(push.last_ok)
        self.assertIn("FIREBASE_CREDENTIALS_FILE", push.last_summary)


class DeliverNowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("p@example.test", PW)

    def test_reasons_when_push_is_not_possible(self):
        with override_settings(FIREBASE_CREDENTIALS_FILE=""):
            self.assertIn("not set", push_problem())
        with override_settings(FIREBASE_CREDENTIALS_FILE="/nowhere/key.json"):
            self.assertIn("does not exist", push_problem())
        with tempfile.NamedTemporaryFile(suffix=".json") as key, override_settings(FIREBASE_CREDENTIALS_FILE=key.name):
            note, _ = create_notification(self.user, "test", "T", "B", "test:1")
            sent, problems = deliver_now(note)
            self.assertEqual(sent, 0)
            self.assertIn("no phone registered", problems[0])

    def test_sends_to_registered_phone(self):
        PushDevice.objects.create(user=self.user, token="tok")
        note, _ = create_notification(self.user, "test", "T", "B", "test:2")
        fake = MagicMock()
        fake.UnregisteredError = type("UnregisteredError", (Exception,), {})
        with tempfile.NamedTemporaryFile(suffix=".json") as key, override_settings(FIREBASE_CREDENTIALS_FILE=key.name), \
                patch("notifications.tasks._firebase", return_value=fake), \
                patch("notifications.tasks.push_message", return_value="msg"):
            self.assertEqual(deliver_now(note), (1, []))
        fake.send.assert_called_once_with("msg")
        self.assertEqual(PushDelivery.objects.get(notification=note).state, PushDelivery.State.SENT)


class PortalNotificationsPageTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("staff@example.test", PW, role=Role.ADMIN, is_staff=True)
        self.employee = User.objects.create_user("emp@example.test", PW)
        self.client.force_login(self.staff)

    def test_page_explains_what_is_missing(self):
        with override_settings(FIREBASE_CREDENTIALS_FILE=""):
            page = self.client.get(reverse("portal:notifications"))
        self.assertContains(page, "Set up the scheduler")
        self.assertContains(page, "manage.py run_scheduled_jobs")
        self.assertContains(page, "FIREBASE_CREDENTIALS_FILE is not set")
        self.assertContains(page, "0 of 1 employees")
        self.assertContains(self.client.get(reverse("portal:dashboard")), 'title="Notifications need attention"')

    def test_run_now_and_test_send(self):
        with override_settings(FIREBASE_CREDENTIALS_FILE=""):
            response = self.client.post(reverse("portal:notifications"), {"action": "run"}, follow=True)
            self.assertContains(response, "Ran the scheduled jobs")
            self.assertNotContains(response, "Set up the scheduler")
            response = self.client.post(reverse("portal:notifications"),
                                        {"action": "test", "email": "EMP@example.test"}, follow=True)
        self.assertContains(response, "Not sent to emp@example.test")
        self.assertTrue(Notification.objects.filter(user=self.employee, kind="test").exists())
        response = self.client.post(reverse("portal:notifications"), {"action": "test", "email": "nobody@x.test"},
                                    follow=True)
        self.assertContains(response, "No active account uses nobody@x.test")

    def test_employees_cannot_open_it(self):
        self.client.force_login(self.employee)
        self.assertEqual(self.client.get(reverse("portal:notifications")).status_code, 403)

    def test_publishing_in_portal_announces_straight_away(self):
        today = timezone.localdate()
        response = self.client.post(reverse("portal:program_new"), {
            "title": "Hydrate", "summary": "Drink water", "daily_action": "Drink a glass", "focus_goal": "all",
            "start_date": today, "end_date": today, "is_published": "on"}, follow=True)
        self.assertContains(response, "1 employee will get a notification about it.")
        self.assertTrue(Notification.objects.filter(user=self.employee, kind="new_program").exists())
