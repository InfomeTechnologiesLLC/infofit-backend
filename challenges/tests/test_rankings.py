import uuid
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Department, Role, Team, User
from challenges.models import Challenge, ChallengeEnrollment, PointsLedger
from fitness.models import DailyActivity, FitnessAccount, FitnessSource

PW = "Strong-passphrase-123"


class RankingsTests(TestCase):
    def setUp(self):
        self.today = timezone.localdate()
        dept = Department.objects.create(name="Eng")
        self.falcons = Team.objects.create(name="Falcons", department=dept)
        self.tigers = Team.objects.create(name="Tigers", department=dept)
        self.me = self.person("me@x.test", "Asha", "Menon", self.falcons, [3000, 4000])
        self.top = self.person("top@x.test", "Joe", "Benny", self.tigers, [12000])
        self.shy = self.person("shy@x.test", "Ramya", "Cheriyan", self.falcons, [9000])
        self.shy.profile.show_in_rankings = False
        self.shy.profile.save()
        self.person("none@x.test", "No", "Data", self.tigers, [])
        User.objects.create_user("hr@x.test", PW, role=Role.HR_ADMIN, is_staff=True)
        self.api = APIClient()
        self.api.force_authenticate(self.me)

    def person(self, email, first, last, team, steps_by_day):
        user = User.objects.create_user(email, PW, first_name=first, last_name=last)
        user.profile.team = team
        user.profile.save()
        source = FitnessSource.objects.create(user=user, installation_id=uuid.uuid4())
        FitnessAccount.objects.create(user=user, active_source=source, time_zone="UTC")
        for back, steps in enumerate(steps_by_day):
            DailyActivity.objects.create(user=user, source=source, local_date=self.today - timedelta(days=back),
                                         time_zone="UTC", steps=steps, metric_status={"steps": "available"},
                                         observed_at=timezone.now())
        return user

    def test_steps_today_with_names_privacy_and_my_row(self):
        data = self.api.get("/api/v1/challenges/rankings/?board=steps_today").json()
        self.assertEqual([(r["rank"], r["name"], r["value"]) for r in data["rows"]],
                         [(1, "Joe B.", 12000), (2, "Colleague", 9000), (3, "You", 3000)])
        self.assertEqual(data["me"]["rank"], 3)
        self.assertEqual(data["rows"][0]["detail"], "Tigers")
        self.assertEqual(data["rows"][0]["value_label"], "12,000 steps")
        self.assertTrue(data["show_my_name"])
        self.assertIn("sync", data["note"])

    def test_teams_use_average_per_member_with_steps(self):
        data = self.api.get("/api/v1/challenges/rankings/?board=teams").json()
        week_start = self.today - timedelta(days=self.today.weekday())
        my_steps = 3000 + (4000 if self.today - timedelta(days=1) >= week_start else 0)
        falcons = (my_steps + 9000) / 2
        expected = sorted([("Falcons", round(falcons)), ("Tigers", 12000)], key=lambda t: -t[1])
        self.assertEqual([(r["name"], r["value"]) for r in data["rows"]], expected)
        self.assertTrue(next(r for r in data["rows"] if r["name"] == "Falcons")["is_me"])

    def test_points_board_and_empty_message(self):
        data = self.api.get("/api/v1/challenges/rankings/?board=points").json()
        self.assertEqual(data["rows"], [])
        self.assertIn("No points yet", data["message"])
        challenge = Challenge.objects.create(slug="c", title="C", start_date=self.today, end_date=self.today,
                                             target_steps=1, reward_points=7, is_published=True)
        enrollment = ChallengeEnrollment.objects.create(challenge=challenge, user=self.me, joined_local_date=self.today)
        PointsLedger.objects.create(enrollment=enrollment, user=self.me, delta=7, revision=1, reason="award")
        data = self.api.get("/api/v1/challenges/rankings/?board=points").json()
        self.assertEqual(data["me"]["value_label"], "7 points")

    def test_hide_my_name_and_validation(self):
        self.assertEqual(self.api.post("/api/v1/challenges/rankings/", {"show_my_name": False}, format="json").json(),
                         {"show_my_name": False})
        other = APIClient()
        other.force_authenticate(self.top)
        names = [r["name"] for r in other.get("/api/v1/challenges/rankings/?board=steps_today").json()["rows"]]
        self.assertEqual(names, ["You", "Colleague", "Colleague"])
        self.assertEqual(self.api.get("/api/v1/challenges/rankings/?board=nope").status_code, 400)
        self.assertEqual(self.api.post("/api/v1/challenges/rankings/", {"show_my_name": "yes"}, format="json").status_code, 400)
