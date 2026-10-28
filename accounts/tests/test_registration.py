from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APITestCase

from accounts.models import Department, EmployeeProfile, Role, User

PW = "Str0ng-Passphrase!9"


class RegistrationTests(APITestCase):
    def setUp(self):
        cache.clear()  # throttle counters
        self.sales = Department.objects.create(name="Sales")

    def register(self, **overrides):
        body = {"full_name": "Asha Menon", "email": "Asha@Example.test", "password": PW, "department": self.sales.id}
        body.update(overrides)
        return self.client.post("/api/v1/auth/register/", body, format="json")

    def test_sign_up_creates_an_employee_and_signs_them_in(self):
        response = self.register()
        self.assertEqual(response.status_code, 201)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)
        user = User.objects.get(email="asha@example.test")
        self.assertEqual((user.first_name, user.last_name, user.role), ("Asha", "Menon", Role.EMPLOYEE))
        self.assertFalse(user.is_staff)
        profile = EmployeeProfile.objects.get(user=user)
        self.assertEqual(profile.department, self.sales)
        self.assertIsNone(profile.team)
        # The tokens work, and the new person sees the welcome tour.
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")
        me = self.client.get("/api/v1/profile/")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.data["department"], "Sales")
        self.assertFalse(me.data["onboarding_completed"])

    def test_role_cannot_be_chosen_at_sign_up(self):
        self.assertEqual(self.register(role="super_admin", is_staff=True).status_code, 201)
        user = User.objects.get(email="asha@example.test")
        self.assertEqual(user.role, Role.EMPLOYEE)
        self.assertFalse(user.is_staff or user.is_superuser)

    def test_existing_email_is_refused_whatever_its_case(self):
        User.objects.create_user(email="asha@example.test", password=PW)
        response = self.register(email="ASHA@example.test")
        self.assertEqual(response.status_code, 400)
        self.assertIn("already exists", str(response.data))

    def test_weak_password_and_missing_department_are_refused(self):
        self.assertEqual(self.register(password="password").status_code, 400)
        response = self.register(department=None)
        self.assertEqual(response.status_code, 400)
        self.assertIn("department", str(response.data))

    def test_department_is_optional_until_hr_creates_one(self):
        Department.objects.all().delete()
        self.assertEqual(self.register(department=None).status_code, 201)

    def test_departments_list_is_public_and_says_if_sign_up_is_open(self):
        response = self.client.get("/api/v1/auth/departments/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["registration_open"])
        self.assertEqual(response.data["results"], [{"id": self.sales.id, "name": "Sales"}])

    @override_settings(ALLOW_SELF_REGISTRATION=False)
    def test_sign_up_can_be_switched_off(self):
        response = self.register()
        self.assertEqual(response.status_code, 403)
        self.assertIn("ask HR", str(response.data))
        self.assertFalse(User.objects.filter(email="asha@example.test").exists())
        self.assertFalse(self.client.get("/api/v1/auth/departments/").data["registration_open"])
