import json
from unittest.mock import patch

from django.test import TestCase, override_settings

from accounts.models import Department, EmployeeProfile, Role, User
from portal import ai

PW = "Portal-Passphrase!42"


class PortalAiTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("staff-ai@example.test", PW, role=Role.ADMIN, is_staff=True)
        self.client.force_login(self.staff)

    def test_too_few_employees_shows_no_figures(self):
        User.objects.create_user("one@example.test", PW)
        self.assertFalse(ai.insight_dataset()["available"])
        response = self.client.get("/portal/insights/")
        self.assertContains(response, "Fewer than 5 employees")

    def test_dataset_has_no_names_and_merges_small_groups(self):
        big, small = Department.objects.create(name="Sales"), Department.objects.create(name="Legal")
        for i in range(6):
            u = User.objects.create_user(f"s{i}@example.test", PW, first_name=f"Person{i}")
            EmployeeProfile.objects.update_or_create(user=u, defaults={"department": big})
        for i in range(2):
            u = User.objects.create_user(f"l{i}@example.test", PW)
            EmployeeProfile.objects.update_or_create(user=u, defaults={"department": small})
        data = ai.insight_dataset()
        text = json.dumps(data, default=str)
        self.assertEqual([d["department"] for d in data["departments"]], ["Sales"])
        self.assertNotIn("Legal", text)
        self.assertNotIn("Person", text)
        self.assertNotIn("@example.test", text)

    @override_settings(GEMINI_API_KEY="k")
    @patch("portal.ai.complete_json", return_value={"title": "Ramadan Steps", "description": "Walk gently.", "evil": "x"})
    def test_draft_endpoint_returns_only_known_fields(self, _):
        response = self.client.post("/portal/ai/draft/", {"kind": "challenge", "brief": "gentle ramadan walking"})
        self.assertEqual(response.json(), {"draft": {"title": "Ramadan Steps", "description": "Walk gently."}})
        self.assertEqual(self.client.post("/portal/ai/draft/", {"kind": "nope", "brief": "x y z"}).status_code, 400)

    def test_employees_cannot_use_staff_ai(self):
        employee = User.objects.create_user("emp-ai@example.test", PW)
        self.client.force_login(employee)
        self.assertEqual(self.client.post("/portal/ai/draft/", {"kind": "challenge", "brief": "abc"}).status_code, 403)
        self.assertEqual(self.client.get("/portal/insights/").status_code, 403)
