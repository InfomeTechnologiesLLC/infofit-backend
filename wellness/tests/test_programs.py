from datetime import timedelta

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APITestCase

from accounts.models import Role, User
from challenges.views import local_today
from fitness.models import FitnessAccount
from wellness.models import ProgramCheckIn, ProgramEnrollment, WellnessProgram


def make_program(today, **kwargs):
    values = {
        "title": "Daily stretch", "summary": "A gentle daily routine",
        "daily_action": "Stretch for five minutes if comfortable.",
        "start_date": today - timedelta(days=1), "end_date": today + timedelta(days=5),
        "is_published": True,
    }
    values.update(kwargs)
    return WellnessProgram.objects.create(**values)


class ProgramApiTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("asha@wellness.test", "Strong-passphrase-123")
        self.other = User.objects.create_user("bilal@wellness.test", "Strong-passphrase-123")
        self.client.force_authenticate(user=self.user)

    def test_all_published_programs_show_and_relevant_ones_are_recommended_first(self):
        today = local_today(self.user)
        matched = make_program(today, title="Endurance", focus_goal="endurance")
        all_goals = make_program(today, title="General")
        make_program(today, title="Hidden", is_published=False)
        other = make_program(today, title="Other goal", focus_goal="stress")
        make_program(today, title="Expired", end_date=today - timedelta(days=1))
        self.user.profile.fitness_goal = "endurance"
        self.user.profile.save()
        response = self.client.get("/api/v1/wellness/programs/")
        self.assertEqual(response.status_code, 200)
        # Programs for another goal are shown too (they used to be hidden), after the recommended one.
        self.assertEqual([row["id"] for row in response.data], [matched.id, all_goals.id, other.id])
        self.assertTrue(response.data[0]["recommended"])
        self.assertFalse(response.data[1]["recommended"])
        self.assertFalse(response.data[2]["recommended"])
        self.assertFalse(response.data[0]["joined"])
        self.assertEqual(response.data[0]["check_in_count"], 0)

    def test_join_and_daily_check_in_are_idempotent_and_user_scoped(self):
        today = local_today(self.user)
        program = make_program(today)
        join_url = f"/api/v1/wellness/programs/{program.id}/join/"
        check_url = f"/api/v1/wellness/programs/{program.id}/check-in/"
        self.assertEqual(self.client.post(check_url).status_code, 400)
        self.assertEqual(self.client.post(join_url).status_code, 201)
        self.assertEqual(self.client.post(join_url).status_code, 200)
        first = self.client.post(check_url)
        self.assertEqual(first.status_code, 201)
        self.assertTrue(first.data["checked_in_today"])
        self.assertEqual(self.client.post(check_url).status_code, 200)
        self.assertEqual(ProgramEnrollment.objects.filter(program=program, user=self.user).count(), 1)
        self.assertEqual(ProgramCheckIn.objects.count(), 1)
        self.client.force_authenticate(user=self.other)
        other_list = self.client.get("/api/v1/wellness/programs/").data
        self.assertFalse(other_list[0]["joined"])
        self.assertEqual(other_list[0]["check_in_count"], 0)
        self.assertEqual(self.client.post(check_url).status_code, 400)

    def test_unpublished_future_and_wrong_goal_cannot_be_joined(self):
        today = local_today(self.user)
        hidden = make_program(today, is_published=False)
        future = make_program(today, start_date=today + timedelta(days=2))
        wrong_goal = make_program(today, focus_goal="stress")
        self.assertEqual(self.client.post(f"/api/v1/wellness/programs/{hidden.id}/join/").status_code, 404)
        self.assertEqual(self.client.post(f"/api/v1/wellness/programs/{future.id}/join/").status_code, 400)
        self.assertEqual(self.client.post(f"/api/v1/wellness/programs/{wrong_goal.id}/join/").status_code, 404)
        self.assertEqual(ProgramEnrollment.objects.count(), 0)

    def test_check_in_uses_employee_local_date(self):
        account = FitnessAccount.objects.create(user=self.user, time_zone="Pacific/Kiritimati")
        today = local_today(self.user)
        program = make_program(today)
        self.client.post(f"/api/v1/wellness/programs/{program.id}/join/")
        self.client.post(f"/api/v1/wellness/programs/{program.id}/check-in/")
        self.assertEqual(ProgramCheckIn.objects.get().local_date, today)
        account.refresh_from_db()
        self.assertEqual(account.time_zone, "Pacific/Kiritimati")

    def test_joined_program_remains_visible_after_profile_goal_changes(self):
        today = local_today(self.user)
        self.user.profile.fitness_goal = "endurance"
        self.user.profile.save()
        program = make_program(today, focus_goal="endurance")
        self.assertEqual(self.client.post(f"/api/v1/wellness/programs/{program.id}/join/").status_code, 201)
        self.user.profile.fitness_goal = "stress"
        self.user.profile.save()
        listing = self.client.get("/api/v1/wellness/programs/").data
        self.assertEqual([row["id"] for row in listing], [program.id])
        self.assertFalse(listing[0]["recommended"])
        self.assertTrue(listing[0]["joined"])
        self.assertEqual(self.client.post(f"/api/v1/wellness/programs/{program.id}/check-in/").status_code, 201)

    def test_authentication_required(self):
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get("/api/v1/wellness/programs/").status_code, 401)


class ProgramAdminTests(TestCase):
    def test_only_staff_business_admins_can_manage_programs(self):
        employee = User.objects.create_user("employee@wellness.test", "Strong-passphrase-123")
        manager = User.objects.create_user("manager@wellness.test", "Strong-passphrase-123", role=Role.TEAM_MANAGER, is_staff=True)
        admin = User.objects.create_user("admin@wellness.test", "Strong-passphrase-123", role=Role.ADMIN, is_staff=True)
        self.client.force_login(employee)
        self.assertEqual(self.client.get("/admin/wellness/wellnessprogram/").status_code, 302)
        self.client.force_login(manager)
        self.assertEqual(self.client.get("/admin/wellness/wellnessprogram/").status_code, 403)
        self.client.force_login(admin)
        self.assertEqual(self.client.get("/admin/wellness/wellnessprogram/").status_code, 200)
