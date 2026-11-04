from uuid import uuid4

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Role, User
from fitness.apple_health import issue_token, token_digest
from fitness.models import AppleHealthConnection, AppleHealthRecord, DailyActivity, FitnessAccount, FitnessSource


PW = "Portal-Passphrase!42"


class EmployeeAppleHealthTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("admin@example.test", PW, role=Role.ADMIN, is_staff=True)
        self.employee = User.objects.create_user("iphone@example.test", PW, role=Role.EMPLOYEE)
        self.client.force_login(self.staff)
        self.url = reverse("portal:employee_apple_health", args=[self.employee.pk])

    def generate(self, **extra):
        return self.client.post(self.url, {"action": "generate", "device_label": "Work iPhone", **extra}, secure=True)

    def upload(self, token, device="iphone-1"):
        return Client().post(reverse("apple_health_sync"), {
            "schema_version": 2, "device_id": device, "export_id": str(uuid4()),
            "generated_at": timezone.now().isoformat(), "timezone": "UTC", "source": "ios-healthkit",
            "date_range": {"start": "2026-11-01T00:00:00Z", "end": "2026-11-02T00:00:00Z"},
            "metrics": [{"id": "healthkit:portal-steps", "type": "step_count", "value": 1250, "unit": "count",
                         "start_at": "2026-11-01T08:00:00Z", "end_at": "2026-11-01T09:00:00Z",
                         "source_name": "iPhone", "metadata": {}}],
            "workouts": [], "deletions": [],
        }, content_type="application/json", HTTP_AUTHORIZATION="Bearer " + token)

    def test_portal_created_employee_can_receive_token_and_sync(self):
        created = self.client.post(reverse("portal:employee_new"), {
            "email": "new-iphone@example.test", "first_name": "New", "last_name": "iPhone",
            "is_active": "on", "password1": PW, "password2": PW,
        })
        self.employee = User.objects.get(email="new-iphone@example.test")
        self.url = reverse("portal:employee_apple_health", args=[self.employee.pk])
        self.assertRedirects(created, reverse("portal:employee_detail", args=[self.employee.pk]))
        self.assertContains(self.client.get(created.url), self.url)
        other = User.objects.create_user("other@example.test", PW)
        other_connection, _ = issue_token(other)
        page = self.client.get(self.url, secure=True)
        self.assertContains(page, "Not configured")
        self.assertContains(page, "https://testserver")
        self.assertFalse(AppleHealthConnection.objects.filter(user=self.employee).exists())

        with self.assertLogs("infome", level="INFO") as logs:
            response = self.generate(user_id=other.pk)
        self.assertEqual(response.status_code, 200)
        token = response.context["token"]
        self.assertTrue(token.startswith("ahs_"))
        self.assertContains(response, "Copy setup instructions")
        self.assertContains(response, "Waiting for first sync")
        self.assertIn("no-store", response["Cache-Control"])
        connection = AppleHealthConnection.objects.get(user=self.employee)
        self.assertEqual(connection.token_hash, token_digest(token))
        self.assertEqual(connection.source.device_label, "Work iPhone")
        self.assertNotIn(token, str(logs.output))
        self.assertNotIn(token, str(dict(self.client.session)))
        other_connection.refresh_from_db()
        self.assertEqual(other_connection.source.user_id, other.pk)
        self.assertNotEqual(other_connection.token_hash, token_digest(token))
        later = self.client.get(self.url)
        self.assertNotContains(later, token)
        self.assertNotContains(later, connection.token_hash)
        self.assertContains(later, "Rotate token")
        self.assertEqual(self.upload(token).status_code, 200)
        day = DailyActivity.objects.get(user=self.employee)
        self.assertEqual(day.steps, 1250)
        self.assertFalse(DailyActivity.objects.filter(user=other).exists())
        self.assertContains(self.client.get(self.url), "Connected")

    def test_rotation_invalidates_old_token_but_keeps_device_and_activity(self):
        old_token = self.generate().context["token"]
        self.assertEqual(self.upload(old_token).status_code, 200)
        before = AppleHealthConnection.objects.get(user=self.employee)
        response = self.generate()
        new_token = response.context["token"]
        after = AppleHealthConnection.objects.get(user=self.employee)
        self.assertNotEqual(old_token, new_token)
        self.assertEqual((after.source_id, after.device_id), (before.source_id, "iphone-1"))
        self.assertEqual(self.upload(old_token).status_code, 401)
        self.assertEqual(self.upload(new_token).status_code, 200)
        self.assertEqual(AppleHealthRecord.objects.filter(source=after.source).count(), 1)
        self.assertEqual(DailyActivity.objects.get(user=self.employee).steps, 1250)

    def test_revoking_token_stops_uploads_and_preserves_history(self):
        token = self.generate().context["token"]
        self.assertEqual(self.upload(token).status_code, 200)
        response = self.client.post(self.url, {"action": "revoke"})
        self.assertRedirects(response, self.url)
        self.assertContains(self.client.get(self.url), "Token revoked")
        self.assertEqual(self.upload(token).status_code, 401)
        self.assertEqual(AppleHealthRecord.objects.count(), 1)
        self.assertEqual(DailyActivity.objects.get(user=self.employee).steps, 1250)
        connection = AppleHealthConnection.objects.get(user=self.employee)
        self.assertEqual(connection.token_hash, "")
        self.assertEqual(connection.device_id, "iphone-1")

    def test_android_source_requires_explicit_replacement(self):
        android = FitnessSource.objects.create(user=self.employee, installation_id=uuid4(), device_label="Android")
        account = FitnessAccount.objects.create(user=self.employee, active_source=android)
        old_day = DailyActivity.objects.create(user=self.employee, source=android, local_date="2026-10-31",
            time_zone="UTC", steps=4200, observed_at=timezone.now())
        response = self.generate()
        self.assertContains(response, "already has an active device")
        account.refresh_from_db()
        self.assertEqual(account.active_source_id, android.pk)
        self.assertFalse(AppleHealthConnection.objects.exists())
        response = self.generate(replace_existing="on")
        self.assertIsNotNone(response.context["token"])
        account.refresh_from_db()
        self.assertEqual(account.active_source.kind, "apple_health")
        old_day.refresh_from_db()
        self.assertEqual((old_day.source_id, old_day.steps), (android.pk, 4200))

    def test_new_iphone_resets_binding_only_after_explicit_replacement(self):
        token = self.generate().context["token"]
        self.assertEqual(self.upload(token).status_code, 200)
        original = AppleHealthConnection.objects.get(user=self.employee)
        self.assertContains(self.generate(new_device="on"), "already has an active device")
        current = AppleHealthConnection.objects.get(user=self.employee)
        self.assertEqual((current.source_id, current.device_id, current.token_hash),
                         (original.source_id, original.device_id, original.token_hash))
        response = self.generate(new_device="on", replace_existing="on")
        current.refresh_from_db()
        self.assertNotEqual(current.source_id, original.source_id)
        self.assertEqual(current.device_id, "")
        self.assertEqual(self.upload(token).status_code, 401)
        self.assertEqual(self.upload(response.context["token"], device="iphone-2").status_code, 200)
        self.assertTrue(AppleHealthRecord.objects.filter(source_id=original.source_id).exists())

    def test_staff_authorization_and_employee_target_restrictions(self):
        token = self.generate().context["token"]
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.assertEqual(self.client.post(self.url, {"action": "revoke"}).status_code, 302)
        manager = User.objects.create_user("manager@example.test", PW, role=Role.TEAM_MANAGER, is_staff=True)
        for actor in (self.employee, manager):
            self.client.force_login(actor)
            self.assertEqual(self.client.get(self.url).status_code, 403)
            for action in ("generate", "revoke"):
                self.assertEqual(self.client.post(self.url, {"action": action}).status_code, 403)
        self.client.force_login(self.staff)
        staff_url = reverse("portal:employee_apple_health", args=[self.staff.pk])
        self.assertEqual(self.client.get(staff_url).status_code, 404)
        self.assertEqual(self.client.post(staff_url, {"action": "generate"}).status_code, 404)
        self.assertEqual(AppleHealthConnection.objects.get(user=self.employee).token_hash, token_digest(token))

    def test_token_actions_require_csrf_and_do_not_mutate_on_get(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.staff)
        self.assertEqual(client.post(self.url, {"action": "generate", "device_label": "iPhone"}).status_code, 403)
        self.assertFalse(AppleHealthConnection.objects.exists())
        token = self.generate().context["token"]
        self.assertEqual(client.post(self.url, {"action": "revoke"}).status_code, 403)
        self.assertEqual(client.get(self.url, {"action": "revoke"}).status_code, 200)
        self.assertEqual(client.delete(self.url, HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value).status_code, 405)
        self.assertEqual(AppleHealthConnection.objects.get(user=self.employee).token_hash, token_digest(token))
        response = client.post(self.url, {"action": "revoke"}, HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value)
        self.assertRedirects(response, self.url)
        self.assertEqual(AppleHealthConnection.objects.get(user=self.employee).token_hash, "")

    def test_inactive_employee_cannot_get_new_token_but_can_be_revoked(self):
        token = self.generate().context["token"]
        self.employee.is_active = False
        self.employee.save(update_fields=["is_active"])
        response = self.generate()
        self.assertContains(response, "Reactivate this employee")
        self.assertIsNone(response.context["token"])
        self.assertEqual(AppleHealthConnection.objects.get(user=self.employee).token_hash, token_digest(token))
        self.assertRedirects(self.client.post(self.url, {"action": "revoke"}), self.url)
        self.assertEqual(AppleHealthConnection.objects.get(user=self.employee).token_hash, "")

    def test_invalid_action_or_label_does_not_rotate_token(self):
        token = self.generate().context["token"]
        self.assertEqual(self.client.post(self.url, {"action": "unknown"}).status_code, 400)
        response = self.generate(device_label="x" * 101)
        self.assertIsNone(response.context["token"])
        self.assertIn("device_label", response.context["form"].errors)
        self.assertEqual(AppleHealthConnection.objects.get(user=self.employee).token_hash, token_digest(token))
