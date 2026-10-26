from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APITestCase

from accounts.models import Department, Role, Team, User
from wellness.models import WellnessProgram

PW = "Str0ng-Passphrase!9"


def make_user(email, role=Role.EMPLOYEE, **kw):
    return User.objects.create_user(email=email, password=PW, role=role, **kw)


class AccountAdminTests(TestCase):
    def test_staff_admin_can_manage_teams_without_superuser_access(self):
        admin_user = make_user("admin@infome.test", role=Role.ADMIN, is_staff=True)
        root_user = User.objects.create_superuser("root@infome.test", PW)
        self.client.force_login(admin_user)
        self.assertContains(self.client.get("/admin/"), "Accounts")
        self.assertEqual(self.client.get("/admin/accounts/team/").status_code, 200)
        self.assertEqual(self.client.get("/admin/accounts/user/add/").status_code, 200)
        self.assertEqual(self.client.get(f"/admin/accounts/user/{root_user.pk}/change/").status_code, 403)

    def test_staff_team_manager_cannot_manage_accounts(self):
        manager = make_user("manager@infome.test", role=Role.TEAM_MANAGER, is_staff=True)
        self.client.force_login(manager)
        self.assertNotContains(self.client.get("/admin/"), "Accounts")
        self.assertEqual(self.client.get("/admin/accounts/team/").status_code, 403)

    def test_business_admin_cannot_create_privileged_user(self):
        admin_user = make_user("admin@infome.test", role=Role.ADMIN, is_staff=True)
        self.client.force_login(admin_user)
        response = self.client.post("/admin/accounts/user/add/", {
            "email": "new@infome.test", "password1": PW, "password2": PW,
            "role": Role.SUPER_ADMIN, "is_staff": "on", "is_superuser": "on",
            "_save": "Save",
        })
        self.assertEqual(response.status_code, 302, response.content[:500])
        created = User.objects.get(email="new@infome.test")
        self.assertEqual(created.role, Role.EMPLOYEE)
        self.assertFalse(created.is_staff)
        self.assertFalse(created.is_superuser)

    def test_operations_overview_is_admin_only_and_uses_real_counts(self):
        admin_user = make_user("admin@infome.test", role=Role.ADMIN, is_staff=True)
        employee = make_user("employee@infome.test")
        manager = make_user("manager@infome.test", role=Role.TEAM_MANAGER, is_staff=True)
        Team.objects.create(name="Falcon")
        from django.utils import timezone
        WellnessProgram.objects.create(
            title="Walk break", summary="Take a short break", daily_action="Walk if comfortable.",
            start_date=timezone.localdate(), end_date=timezone.localdate(), is_published=True,
        )
        self.assertEqual(self.client.get("/admin/analytics/").status_code, 302)
        self.client.force_login(employee)
        self.assertEqual(self.client.get("/admin/analytics/").status_code, 302)
        self.client.force_login(manager)
        self.assertEqual(self.client.get("/admin/analytics/").status_code, 403)
        self.client.force_login(admin_user)
        response = self.client.get("/admin/analytics/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Active employees")
        self.assertContains(response, "Teams")
        self.assertIn(("Active employees", 1), response.context["cards"])
        self.assertIn(("Teams", 1), response.context["cards"])
        self.assertIn(("Published wellness programs", 1), response.context["cards"])


class BaseAPITest(APITestCase):
    def setUp(self):
        cache.clear()  # reset throttle counters between tests

    def login(self, email, password=PW):
        return self.client.post(reverse("login"), {"email": email, "password": password}, format="json")

    def auth(self, email):
        r = self.login(email)
        self.assertEqual(r.status_code, 200, r.data)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {r.data['access']}")
        return r.data


class AuthTests(BaseAPITest):
    def setUp(self):
        super().setUp()
        self.u = make_user("a@infome.test", first_name="Asha")

    def test_login_success_returns_tokens_and_user_summary(self):
        r = self.login("A@Infome.test ")  # case/whitespace insensitive
        self.assertEqual(r.status_code, 200)
        self.assertIn("access", r.data)
        self.assertIn("refresh", r.data)
        self.assertEqual(r.data["user"]["role"], "employee")
        self.assertNotIn("password", str(r.data))

    def test_login_wrong_password_is_401_with_standard_error_shape(self):
        r = self.login("a@infome.test", "nope")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.data["error"]["code"], "unauthenticated")

    def test_inactive_user_cannot_login(self):
        self.u.is_active = False
        self.u.save()
        self.assertEqual(self.login("a@infome.test").status_code, 401)

    def test_password_is_hashed(self):
        self.assertNotEqual(self.u.password, PW)
        self.assertTrue(self.u.password.startswith(("pbkdf2_", "argon2", "bcrypt")))

    def test_protected_endpoint_requires_token(self):
        r = self.client.get(reverse("profile"))
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.data["error"]["code"], "unauthenticated")

    def test_refresh_rotates_and_old_refresh_is_blacklisted(self):
        tokens = self.login("a@infome.test").data
        r1 = self.client.post(reverse("token_refresh"), {"refresh": tokens["refresh"]}, format="json")
        self.assertEqual(r1.status_code, 200)
        self.assertNotEqual(r1.data["refresh"], tokens["refresh"])
        r2 = self.client.post(reverse("token_refresh"), {"refresh": tokens["refresh"]}, format="json")
        self.assertEqual(r2.status_code, 401)  # reuse of the rotated token is rejected

    def test_logout_blacklists_refresh_token(self):
        tokens = self.auth("a@infome.test")
        r = self.client.post(reverse("logout"), {"refresh": tokens["refresh"]}, format="json")
        self.assertEqual(r.status_code, 204)
        r2 = self.client.post(reverse("token_refresh"), {"refresh": tokens["refresh"]}, format="json")
        self.assertEqual(r2.status_code, 401)

    def test_logout_with_garbage_token_is_400(self):
        self.auth("a@infome.test")
        r = self.client.post(reverse("logout"), {"refresh": "garbage"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_login_is_rate_limited(self):
        codes = [self.login("a@infome.test", "bad").status_code for _ in range(12)]
        self.assertIn(429, codes)

    def test_password_change(self):
        self.auth("a@infome.test")
        url = reverse("password_change")
        bad = self.client.post(url, {"old_password": "wrong", "new_password": "Another-Str0ng-Pass!7"}, format="json")
        self.assertEqual(bad.status_code, 400)
        weak = self.client.post(url, {"old_password": PW, "new_password": "12345"}, format="json")
        self.assertEqual(weak.status_code, 400)
        ok = self.client.post(url, {"old_password": PW, "new_password": "Another-Str0ng-Pass!7"}, format="json")
        self.assertEqual(ok.status_code, 204)
        self.client.credentials()
        self.assertEqual(self.login("a@infome.test", PW).status_code, 401)
        self.assertEqual(self.login("a@infome.test", "Another-Str0ng-Pass!7").status_code, 200)


class ProfileTests(BaseAPITest):
    def setUp(self):
        super().setUp()
        dept = Department.objects.create(name="Engineering")
        self.team = Team.objects.create(name="Falcon", department=dept)
        self.a = make_user("a@infome.test", first_name="Asha", employee_id="E001")
        self.b = make_user("b@infome.test", first_name="Bilal", employee_id="E002")
        self.a.profile.team = self.team
        self.a.profile.department = dept
        self.a.profile.designation = "Engineer"
        self.a.profile.save()

    def test_profile_profile_created_automatically(self):
        self.assertTrue(hasattr(self.b, "profile"))

    def test_get_own_profile(self):
        self.auth("a@infome.test")
        r = self.client.get(reverse("profile"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["email"], "a@infome.test")
        self.assertEqual(r.data["team"], "Falcon")
        self.assertEqual(r.data["department"], "Engineering")
        self.assertEqual(r.data["daily_step_goal"], 10000)

    def test_user_isolation_each_user_sees_only_own_profile(self):
        self.auth("b@infome.test")
        r = self.client.get(reverse("profile"))
        self.assertEqual(r.data["email"], "b@infome.test")
        self.assertNotEqual(r.data["employee_id"], "E001")

    def test_update_allowed_fields(self):
        self.auth("a@infome.test")
        r = self.client.patch(
            reverse("profile"),
            {"first_name": "Ashaa", "daily_step_goal": 8000, "fitness_goal": "endurance",
             "wellness_preferences": {"reminders": True}},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.data)
        self.a.refresh_from_db()
        self.a.profile.refresh_from_db()
        self.assertEqual(self.a.first_name, "Ashaa")
        self.assertEqual(self.a.profile.daily_step_goal, 8000)
        self.assertEqual(self.a.profile.fitness_goal, "endurance")

    def test_employee_cannot_change_role_email_team_or_designation(self):
        self.auth("a@infome.test")
        r = self.client.patch(
            reverse("profile"),
            {"role": "super_admin", "email": "hacker@x.com", "designation": "CEO", "team": "Other", "employee_id": "E999"},
            format="json",
        )
        self.assertEqual(r.status_code, 200)
        self.a.refresh_from_db()
        self.a.profile.refresh_from_db()
        self.assertEqual(self.a.role, Role.EMPLOYEE)
        self.assertEqual(self.a.email, "a@infome.test")
        self.assertEqual(self.a.employee_id, "E001")
        self.assertEqual(self.a.profile.designation, "Engineer")
        self.assertEqual(self.a.profile.team, self.team)

    def test_validation_errors_use_standard_shape(self):
        self.auth("a@infome.test")
        r = self.client.patch(reverse("profile"), {"daily_step_goal": 5}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["error"]["code"], "validation_error")
        self.assertIn("daily_step_goal", r.data["error"]["details"])

    def test_delete_not_allowed(self):
        self.auth("a@infome.test")
        self.assertEqual(self.client.delete(reverse("profile")).status_code, 405)


class PermissionClassTests(BaseAPITest):
    def test_role_helpers(self):
        from accounts.permissions import IsAdminRole, IsTeamManagerOrAdmin

        class Req:  # minimal stand-in
            def __init__(self, user): self.user = user

        emp = make_user("e@infome.test")
        mgr = make_user("m@infome.test", Role.TEAM_MANAGER)
        hr = make_user("h@infome.test", Role.HR_ADMIN)
        self.assertFalse(IsAdminRole().has_permission(Req(emp), None))
        self.assertFalse(IsAdminRole().has_permission(Req(mgr), None))
        self.assertTrue(IsAdminRole().has_permission(Req(hr), None))
        self.assertTrue(IsTeamManagerOrAdmin().has_permission(Req(mgr), None))
        self.assertFalse(IsTeamManagerOrAdmin().has_permission(Req(emp), None))

    def test_superuser_gets_super_admin_role(self):
        su = User.objects.create_superuser("root@infome.test", PW)
        self.assertEqual(su.role, Role.SUPER_ADMIN)
        self.assertTrue(su.is_staff)


class HealthTests(BaseAPITest):
    def test_health_endpoint(self):
        self.assertEqual(self.client.get("/healthz/").status_code, 200)


class OnboardingTests(APITestCase):
    URL = "/api/v1/profile/onboarding/"

    def setUp(self):
        self.user = make_user("onboard@infome.test")
        self.other = make_user("other-onboard@infome.test")
        self.client.force_authenticate(self.user)

    def test_new_profile_needs_onboarding_then_complete_is_idempotent(self):
        self.assertFalse(self.client.get("/api/v1/profile/").data["onboarding_completed"])
        first = self.client.post(self.URL)
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.data["onboarding_completed"])
        self.user.profile.refresh_from_db()
        stamp = self.user.profile.onboarding_completed_at
        self.assertIsNotNone(stamp)
        self.assertEqual(self.client.post(self.URL).status_code, 200)
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.onboarding_completed_at, stamp)  # not re-stamped

    def test_replay_resets_only_own_flag_and_flag_is_not_client_writable(self):
        self.client.post(self.URL)
        self.client.force_authenticate(self.other)
        self.client.post(self.URL)
        self.client.force_authenticate(self.user)
        self.assertFalse(self.client.delete(self.URL).data["onboarding_completed"])
        self.other.profile.refresh_from_db()
        self.assertIsNotNone(self.other.profile.onboarding_completed_at)
        patched = self.client.patch("/api/v1/profile/", {"onboarding_completed": True}, format="json")
        self.assertFalse(patched.data["onboarding_completed"])

    def test_requires_authentication(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.client.post(self.URL).status_code, 401)
