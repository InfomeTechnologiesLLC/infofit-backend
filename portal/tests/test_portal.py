import io
import tempfile
from datetime import timedelta

from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from accounts.models import Department, Role, Team, User
from challenges.models import Challenge, ChallengeEnrollment, ChallengeProgress, PointsLedger
from community.models import CommunityPost, CommunityReport
from wellness.models import ProgramCheckIn, ProgramEnrollment, WellnessProgram


PW = "Portal-Passphrase!42"


class PortalTests(TestCase):
    def setUp(self):
        media_dir = tempfile.TemporaryDirectory()
        self.addCleanup(media_dir.cleanup)
        media_settings = override_settings(MEDIA_ROOT=media_dir.name)
        media_settings.enable()
        self.addCleanup(media_settings.disable)
        self.staff = User.objects.create_user("staff@example.test", PW, role=Role.ADMIN, is_staff=True)
        self.employee = User.objects.create_user("employee@example.test", PW, role=Role.EMPLOYEE)
        self.manager = User.objects.create_user("manager@example.test", PW, role=Role.TEAM_MANAGER, is_staff=True)
        self.today = timezone.localdate()
        self.client.force_login(self.staff)

    def test_staff_access_and_login(self):
        self.client.logout()
        self.assertRedirects(self.client.get(reverse("portal:dashboard")),
                             reverse("portal:login") + "?next=/portal/", fetch_redirect_response=False)
        response = self.client.post(reverse("portal:login"), {"username": self.staff.email, "password": PW})
        self.assertRedirects(response, reverse("portal:dashboard"))
        self.assertContains(self.client.get(reverse("portal:dashboard")), "Needs your attention")
        self.client.logout()
        response = self.client.post(reverse("portal:login"), {"username": self.employee.email, "password": PW})
        self.assertContains(response, "cannot access the staff portal")
        self.client.force_login(self.employee)
        self.assertEqual(self.client.get(reverse("portal:employees")).status_code, 403)
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(reverse("portal:dashboard")).status_code, 403)

    def test_dashboard_counts_real_participation_without_health_score(self):
        challenge = Challenge.objects.create(slug="walk", title="Walk", start_date=self.today,
            end_date=self.today + timedelta(days=1), target_steps=100, reward_points=5, is_published=True)
        enrollment = ChallengeEnrollment.objects.create(challenge=challenge, user=self.employee,
            joined_local_date=self.today)
        ChallengeProgress.objects.create(enrollment=enrollment, verified_steps=100, has_step_data=True,
            is_complete=True, evaluated_at=timezone.now())
        program = WellnessProgram.objects.create(title="Break", summary="Walk", daily_action="Take a break",
            start_date=self.today, end_date=self.today + timedelta(days=1), is_published=True)
        wellness = ProgramEnrollment.objects.create(program=program, user=self.employee)
        ProgramCheckIn.objects.create(enrollment=wellness, local_date=self.today)
        response = self.client.get(reverse("portal:dashboard"))
        self.assertEqual(response.status_code, 200)
        kpis = {k["label"]: k["value"] for k in response.context["kpis"]}
        self.assertEqual(kpis["Taking part now"], 1)
        self.assertEqual(kpis["Active in last 7 days"], 1)
        self.assertEqual(kpis["Wellness check-ins today"], 1)
        live = {row["challenge"].pk: row for row in response.context["live"]}
        self.assertEqual((live[challenge.pk]["joined"], live[challenge.pk]["completed"]), (1, 1))
        detail = self.client.get(reverse("portal:challenge_detail", args=[challenge.pk]))
        self.assertContains(detail, self.employee.email.split("@")[0][:1])
        self.assertContains(detail, "Completed")
        self.assertNotContains(response, "healthy employee")
        employee_page = self.client.get(reverse("portal:employees"))
        self.assertContains(employee_page, self.employee.email)
        self.assertContains(employee_page, "Active this week")

    def test_navigation_and_management_forms_render(self):
        for name in ("dashboard", "employees", "departments", "teams", "challenges",
                     "programs", "posts", "reports", "employee_new", "department_new",
                     "team_new", "challenge_new", "program_new"):
            with self.subTest(name=name):
                response = self.client.get(reverse(f"portal:{name}"))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Staff portal")
        self.assertRedirects(self.client.get("/"), reverse("portal:dashboard"), fetch_redirect_response=False)

    def test_apple_health_partial_upload_distinguishes_step_dates_from_recent_calories(self):
        from uuid import uuid4
        from fitness.models import DailyActivity, FitnessAccount, FitnessSource
        source = FitnessSource.objects.create(user=self.employee, installation_id=uuid4(), kind="apple_health",
                                               last_successful_sync_at=timezone.now())
        FitnessAccount.objects.create(user=self.employee, active_source=source)
        historical = self.today.replace(day=1) - timedelta(days=3)
        DailyActivity.objects.create(user=self.employee, source=source, local_date=historical, time_zone="UTC",
                                     steps=17208, metric_status={"steps": "available"}, observed_at=timezone.now())
        today = DailyActivity.objects.create(user=self.employee, source=source, local_date=self.today, time_zone="UTC",
                                             active_kcal=51, metric_status={"steps": "no_data", "active_kcal": "available"},
                                             observed_at=timezone.now())
        url = reverse("portal:employee_detail", args=[self.employee.pk])
        response = self.client.get(url)
        self.assertContains(response, "Last Apple Health sync")
        self.assertContains(response, "active Apple Health source")
        self.assertContains(response, "No step readings received for today.")
        self.assertContains(response, '?month=' + historical.strftime("%Y-%m"))
        self.assertEqual(response.context["step_coverage"], {"first_date": historical, "last_date": historical, "days": 1})
        self.assertIsNone(response.context["step_summary"]["month"])
        self.assertEqual(response.context["step_summary"]["all"]["steps"], 17208)
        today.steps = 1000
        today.metric_status["steps"] = "available"
        today.save()
        response = self.client.get(url)
        self.assertNotContains(response, "No step readings received for today.")
        self.assertEqual(response.context["step_summary"]["month"]["steps"], 1000)

    def test_department_team_and_program_creation(self):
        self.assertEqual(self.client.post(reverse("portal:department_new"), {"name": "Engineering"}).status_code, 302)
        department = Department.objects.get(name="Engineering")
        self.assertEqual(self.client.post(reverse("portal:team_new"), {
            "name": "Blue", "description": "Engineering team", "department": department.pk,
        }).status_code, 302)
        self.assertEqual(Team.objects.get(name="Blue").department, department)
        self.assertEqual(self.client.post(reverse("portal:program_new"), {
            "title": "Take a walk", "summary": "Short walking break", "daily_action": "Walk if comfortable",
            "focus_goal": "all", "start_date": self.today,
            "end_date": self.today + timedelta(days=3), "is_published": "on",
        }).status_code, 302)
        self.assertTrue(WellnessProgram.objects.get(title="Take a walk").is_published)

    def test_staff_step_rankings_exclude_manual_entries(self):
        from uuid import uuid4
        from fitness.models import DailyActivity, FitnessAccount, FitnessSource

        android = FitnessSource.objects.create(user=self.employee, installation_id=uuid4())
        FitnessAccount.objects.create(user=self.employee, active_source=android)
        iphone_user = User.objects.create_user("iphone-ranking@example.test", PW)
        iphone = FitnessSource.objects.create(user=iphone_user, installation_id=uuid4(), kind="apple_health")
        FitnessAccount.objects.create(user=iphone_user, active_source=iphone)
        DailyActivity.objects.create(user=self.employee, source=android, local_date=self.today, time_zone="UTC",
            steps=15000, manual_steps=14000, metric_status={"steps": "available"}, observed_at=timezone.now())
        DailyActivity.objects.create(user=iphone_user, source=iphone, local_date=self.today, time_zone="UTC",
            steps=20000, verified_metrics={"steps": 2000}, metric_status={"steps": "available"}, observed_at=timezone.now())
        # A summary without a normalized total must not boost either the score or its recorded-day count.
        DailyActivity.objects.create(user=iphone_user, source=iphone, local_date=self.today - timedelta(days=40),
            time_zone="UTC", steps=50000, verified_metrics={"steps": None}, metric_status={"steps": "available"},
            observed_at=timezone.now())
        for period in ("today", "week", "month", "all"):
            with self.subTest(period=period):
                response = self.client.get(reverse("portal:points"), {"sort": "steps", "period": period})
                self.assertEqual(response.status_code, 200)
                self.assertEqual([(row["user"].pk, row["steps"], row["step_days"]) for row in response.context["rows"]],
                                 [(iphone_user.pk, 2000, 1), (self.employee.pk, 1000, 1)])
        summary = self.client.get(reverse("portal:employee_detail", args=[self.employee.pk]))
        self.assertEqual(summary.context["step_summary"]["today"]["steps"], 15000)

    def test_employee_creation_and_edit_cannot_grant_admin_access(self):
        team = Team.objects.create(name="Blue")
        response = self.client.post(reverse("portal:employee_new"), {
            "email": "new@example.test", "first_name": "New", "last_name": "Employee",
            "employee_id": "E123", "team": team.pk, "is_active": "on",
            "password1": PW, "password2": PW,
            "role": Role.SUPER_ADMIN, "is_staff": "on", "is_superuser": "on",
        })
        self.assertEqual(response.status_code, 302, response.content[:500])
        created = User.objects.get(email="new@example.test")
        self.assertEqual(created.role, Role.EMPLOYEE)
        self.assertFalse(created.is_staff)
        self.assertFalse(created.is_superuser)
        self.assertTrue(created.check_password(PW))
        self.assertEqual(created.profile.team, team)
        self.assertEqual(self.client.get(reverse("portal:employee_edit", args=[self.staff.pk])).status_code, 404)
        self.client.post(reverse("portal:employee_edit", args=[created.pk]), {
            "email": "new@example.test", "first_name": "Updated", "last_name": "Employee",
            "employee_id": "E123", "team": team.pk, "is_active": "on", "role": Role.ADMIN,
        })
        created.refresh_from_db()
        self.assertEqual(created.first_name, "Updated")
        self.assertEqual(created.role, Role.EMPLOYEE)

    def test_challenge_rules_lock_after_publication(self):
        challenge = Challenge.objects.create(slug="walk", title="Walk", start_date=self.today,
            end_date=self.today + timedelta(days=1), target_steps=100, reward_points=5, is_published=True)
        response = self.client.post(reverse("portal:challenge_edit", args=[challenge.pk]), {
            "slug": "changed", "title": "Walk more", "kind": "steps", "start_date": self.today,
            "end_date": self.today + timedelta(days=1), "target_steps": 9999,
            "reward_points": 900, "badge_title": "", "is_published": "on",
        })
        self.assertEqual(response.status_code, 302, response.content[:500])
        challenge.refresh_from_db()
        self.assertEqual(challenge.title, "Walk more")
        self.assertEqual(challenge.slug, "walk")
        self.assertEqual(challenge.target_steps, 100)
        self.assertEqual(challenge.reward_points, 5)

    def test_convert_running_challenge_corrects_old_step_award_and_ranks_staff(self):
        challenge = Challenge.objects.create(slug="run", title="Run", start_date=self.today,
            end_date=self.today + timedelta(days=1), target_steps=100, reward_points=5, is_published=True)
        enrollment = ChallengeEnrollment.objects.create(challenge=challenge, user=self.employee,
            joined_local_date=self.today)
        ChallengeProgress.objects.create(enrollment=enrollment, verified_steps=200,
            has_step_data=True, is_complete=True, revision=1)
        PointsLedger.objects.create(enrollment=enrollment, user=self.employee,
            delta=5, revision=1, reason="award")
        response = self.client.post(reverse("portal:challenge_edit", args=[challenge.pk]), {
            "title": "Run", "kind": "running", "target_running_minutes": 20,
            "is_published": "on",
        })
        self.assertEqual(response.status_code, 302, response.content[:500])
        challenge.refresh_from_db()
        self.assertEqual(challenge.kind, Challenge.Kind.RUNNING)
        self.assertEqual(sum(PointsLedger.objects.values_list("delta", flat=True)), 0)
        ranking = self.client.get(reverse("portal:points"))
        self.assertContains(ranking, self.employee.email)
        self.assertContains(ranking, "#1")
        self.client.force_login(self.employee)
        self.assertEqual(self.client.get(reverse("portal:points")).status_code, 403)

    def test_report_review_photo_and_post_only_actions(self):
        image = Image.new("RGB", (2, 2), "blue")
        data = io.BytesIO()
        image.save(data, format="JPEG")
        post = CommunityPost.objects.create(author=self.employee, body="Review me",
            visibility=CommunityPost.Visibility.COMPANY)
        post.image.save("portal-test.jpg", data)
        report = CommunityReport.objects.create(post=post, reporter=self.staff, reason="Spam")
        self.assertContains(self.client.get(reverse("portal:report_detail", args=[report.pk])), "Review me")
        photo = self.client.get(reverse("portal:post_photo", args=[post.pk]))
        self.assertEqual(photo.status_code, 200)
        self.assertEqual(photo["Cache-Control"], "private, no-store")
        self.assertTrue(b"".join(photo.streaming_content))
        self.client.force_login(self.employee)
        self.assertEqual(self.client.get(reverse("portal:post_photo", args=[post.pk])).status_code, 403)
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse("portal:hide_reported_post", args=[report.pk])).status_code, 405)
        self.assertEqual(self.client.post(reverse("portal:hide_reported_post", args=[report.pk])).status_code, 302)
        post.refresh_from_db()
        report.refresh_from_db()
        self.assertEqual(post.status, CommunityPost.Status.HIDDEN)
        self.assertEqual(report.status, CommunityReport.Status.RESOLVED)

    def test_moderation_action_requires_csrf(self):
        post = CommunityPost.objects.create(author=self.employee, body="Visible",
            visibility=CommunityPost.Visibility.COMPANY)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.staff)
        self.assertEqual(client.post(reverse("portal:hide_post", args=[post.pk])).status_code, 403)
        post.refresh_from_db()
        self.assertEqual(post.status, CommunityPost.Status.PUBLISHED)


class EmployeeDeleteTests(TestCase):
    def setUp(self):
        media_dir = tempfile.TemporaryDirectory()
        self.addCleanup(media_dir.cleanup)
        media_settings = override_settings(MEDIA_ROOT=media_dir.name)
        media_settings.enable()
        self.addCleanup(media_settings.disable)
        self.staff = User.objects.create_user("staff@example.test", PW, role=Role.ADMIN, is_staff=True)
        self.employee = User.objects.create_user("gone@example.test", PW, role=Role.EMPLOYEE)
        self.other = User.objects.create_user("stays@example.test", PW, role=Role.EMPLOYEE)
        self.client.force_login(self.staff)
        today = timezone.localdate()
        challenge = Challenge.objects.create(slug="walk", title="Walk", start_date=today,
            end_date=today + timedelta(days=1), target_steps=100, reward_points=5, is_published=True)
        enrollment = ChallengeEnrollment.objects.create(challenge=challenge, user=self.employee, joined_local_date=today)
        PointsLedger.objects.create(enrollment=enrollment, user=self.employee, delta=5, revision=1, reason="award")
        self.challenge = challenge

    def test_delete_needs_matching_email_then_removes_everything(self):
        url = reverse("portal:employee_delete", args=[self.employee.pk])
        page = self.client.get(url)
        self.assertContains(page, "be undone")
        self.assertContains(page, "Points entries")
        from django.core.files.base import ContentFile
        post = CommunityPost.objects.create(author=self.employee, body="Bye",
            visibility=CommunityPost.Visibility.COMPANY)
        post.image.save("bye.jpg", ContentFile(b"jpeg"))
        self.employee.profile_image.save("me.jpg", ContentFile(b"jpeg"))
        storage, names = post.image.storage, [post.image.name, self.employee.profile_image.name]
        self.assertTrue(all(storage.exists(name) for name in names))
        response = self.client.post(url, {"confirm_email": "wrong@example.test"})
        self.assertContains(response, "does not match")
        self.assertTrue(User.objects.filter(pk=self.employee.pk).exists())
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(url, {"confirm_email": " GONE@example.test "})
        self.assertRedirects(response, reverse("portal:employees"))
        self.assertFalse(User.objects.filter(pk=self.employee.pk).exists())
        self.assertFalse(PointsLedger.objects.exists())
        self.assertFalse(ChallengeEnrollment.objects.exists())
        self.assertTrue(Challenge.objects.filter(pk=self.challenge.pk).exists())
        self.assertTrue(User.objects.filter(pk=self.other.pk).exists())
        self.assertFalse(any(storage.exists(name) for name in names))

    def test_staff_accounts_cannot_be_deleted_or_deactivated_here(self):
        self.assertEqual(self.client.get(reverse("portal:employee_delete", args=[self.staff.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("portal:employee_toggle_active", args=[self.staff.pk])).status_code, 404)
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(reverse("portal:employee_delete", args=[self.employee.pk]),
                                          {"confirm_email": self.employee.email}).status_code, 403)
        self.assertTrue(User.objects.filter(pk=self.employee.pk).exists())

    def test_deactivate_and_reactivate(self):
        url = reverse("portal:employee_toggle_active", args=[self.employee.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        self.client.post(url)
        self.employee.refresh_from_db()
        self.assertFalse(self.employee.is_active)
        self.assertContains(self.client.get(reverse("portal:employee_detail", args=[self.employee.pk])), "Reactivate")
        self.client.post(url)
        self.employee.refresh_from_db()
        self.assertTrue(self.employee.is_active)
        listing = self.client.get(reverse("portal:employees"))
        self.assertContains(listing, reverse("portal:employee_delete", args=[self.employee.pk]))
        self.assertContains(listing, reverse("portal:employee_edit", args=[self.employee.pk]))


class EmployeePasswordTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("staff@example.test", PW, role=Role.ADMIN, is_staff=True)
        self.employee = User.objects.create_user("worker@example.test", PW, role=Role.EMPLOYEE)
        self.client.force_login(self.staff)
        self.url = reverse("portal:employee_password", args=[self.employee.pk])

    def test_admin_sets_new_password_and_signs_employee_out(self):
        from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken
        from rest_framework_simplejwt.tokens import RefreshToken
        refresh = RefreshToken.for_user(self.employee)
        page = self.client.get(self.url)
        self.assertContains(page, "Set a new password")
        self.assertContains(self.client.get(reverse("portal:employee_detail", args=[self.employee.pk])), self.url)
        response = self.client.post(self.url, {"password1": "Fresh-Start-Pass-91", "password2": "Fresh-Start-Pass-91",
                                               "sign_out": "on"})
        self.assertRedirects(response, reverse("portal:employee_detail", args=[self.employee.pk]))
        self.employee.refresh_from_db()
        self.assertTrue(self.employee.check_password("Fresh-Start-Pass-91"))
        self.assertFalse(self.employee.check_password(PW))
        self.assertTrue(BlacklistedToken.objects.filter(token__jti=refresh["jti"]).exists())
        api = Client()
        self.assertEqual(api.post(reverse("token_refresh"), {"refresh": str(refresh)},
                                  content_type="application/json").status_code, 401)

    def test_rejects_mismatch_and_weak_passwords(self):
        response = self.client.post(self.url, {"password1": "Fresh-Start-Pass-91", "password2": "Other-Pass-91"})
        self.assertContains(response, "don&#x27;t match")
        response = self.client.post(self.url, {"password1": "12345678", "password2": "12345678"})
        self.assertEqual(response.status_code, 200)
        self.employee.refresh_from_db()
        self.assertTrue(self.employee.check_password(PW))

    def test_only_employee_accounts_and_only_staff(self):
        self.assertEqual(self.client.get(reverse("portal:employee_password", args=[self.staff.pk])).status_code, 404)
        self.client.force_login(self.employee)
        self.assertEqual(self.client.post(self.url, {"password1": "Fresh-Start-Pass-91",
                                                     "password2": "Fresh-Start-Pass-91"}).status_code, 403)
