import io
import tempfile
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image
from rest_framework_simplejwt.tokens import AccessToken

from accounts.models import EmployeeProfile, Role, Team, User
from challenges.models import BadgeAward, Challenge, ChallengeEnrollment, PointsLedger
from community.models import CommunityComment, CommunityPost, CommunityReaction, CommunityReport
from employee_web.authentication import SESSION_KEY
from fitness.apple_health import issue_token, revoke_token
from fitness.models import DailyActivity, FitnessAccount, FitnessSource


class EmployeeWebTests(TestCase):
    def setUp(self):
        cache.clear()
        media = tempfile.TemporaryDirectory()
        self.addCleanup(media.cleanup)
        settings = override_settings(MEDIA_ROOT=media.name, COMMUNITY_AI_MODERATION=False)
        settings.enable()
        self.addCleanup(settings.disable)
        self.user = User.objects.create_user("iphone@example.test", "A-long-password!", first_name="Asha", last_name="Nair")
        self.other = User.objects.create_user("other@example.test", "Other-password!", first_name="Other")
        self.team = Team.objects.create(name="Team One")
        EmployeeProfile.objects.update_or_create(user=self.user, defaults={"team": self.team})
        EmployeeProfile.objects.update_or_create(user=self.other, defaults={"team": Team.objects.create(name="Team Two")})
        self.connection, self.token = issue_token(self.user)
        self.client = Client(enforce_csrf_checks=True)
        self.today = timezone.localdate()

    def login(self, client=None, token=None):
        client = client or self.client
        client.get(reverse("employee_web:login"))
        return client.post(reverse("employee_web:login"), {"token": token or self.token},
            HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value)

    def write(self, path, data=None, method="post", client=None):
        client = client or self.client
        return getattr(client, method)("/app/api/" + path, data=data or {}, content_type="application/json",
            HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value)

    def challenge(self, **kwargs):
        defaults = {"slug": str(uuid4()), "title": "Walk together", "start_date": self.today,
            "end_date": self.today + timedelta(days=7), "is_published": True,
            "target_steps": 1000, "reward_points": 50, "badge_title": "First win"}
        return Challenge.objects.create(**{**defaults, **kwargs})

    def test_token_login_creates_scoped_session_without_plaintext_or_device_binding(self):
        self.assertRedirects(self.client.get("/app/"), "/app/login/")
        response = self.login()
        self.assertRedirects(response, "/app/")
        self.assertNotIn(self.token, str(dict(self.client.session)))
        self.assertNotIn("_auth_user_id", self.client.session)
        page = self.client.get("/app/")
        self.assertContains(page, "Community")
        self.assertNotContains(page, self.token)
        self.assertIn("no-store", page["Cache-Control"])
        result = self.client.get("/app/api/me/")
        self.assertEqual(result.json()["name"], "Asha Nair")
        self.assertIn("no-store", result["Cache-Control"])
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.device_id, "")

    @override_settings(DEBUG=False, SECURE_SSL_REDIRECT=True)
    def test_assets_work_without_static_proxy_and_only_allow_public_ui_files(self):
        for name, content_type in (("app.css", "text/css"), ("app.js", "text/javascript"), ("login.js", "text/javascript")):
            response = self.client.get(reverse("employee_web:asset", args=[name]), secure=True)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response["Content-Type"].startswith(content_type))
            # Exhaust the test client's iterator so it closes the response
            # without closing MySQL's enclosing TestCase transaction.
            self.assertTrue(b"".join(response.streaming_content))
        self.assertEqual(self.client.get("/app/assets/.env", secure=True).status_code, 404)
        self.assertEqual(self.client.get("/app/assets/views.py", secure=True).status_code, 404)
        self.assertContains(self.client.get("/app/login/", secure=True), "/app/assets/app.css")

    def test_login_validation_and_rate_limit_do_not_echo_tokens(self):
        response = self.login(token="ahs_wrong-token")
        self.assertContains(response, "invalid or no longer active")
        self.assertNotContains(response, "ahs_wrong-token")
        for _ in range(9):
            self.login(token="bad")
        limited = self.login()
        self.assertEqual(limited.status_code, 429)
        self.assertIn("Retry-After", limited)
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_all_employee_api_routes_require_employee_session(self):
        paths = ["me/", "home/", "steps/", "activity/", "rankings/", "challenges/", "challenges/mine/",
            "challenges/1/join/", "community/posts/", "community/posts/photos/", "community/posts/1/",
            "community/posts/1/photo/", "community/posts/1/comments/", "community/posts/1/reactions/like/",
            "community/posts/1/report/"]
        for path in paths:
            with self.subTest(path=path):
                response = self.client.get("/app/api/" + path, HTTP_AUTHORIZATION="Bearer " + self.token)
                self.assertEqual(response.status_code, 401)
        self.assertEqual(self.login(token=str(AccessToken.for_user(self.user))).status_code, 200)
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_csrf_required_for_login_writes_and_logout(self):
        self.assertEqual(self.client.post("/app/login/", {"token": self.token}).status_code, 403)
        self.login()
        challenge = self.challenge()
        post = CommunityPost.objects.create(author=self.other, body="Hello", visibility="company")
        actions = [("post", "rankings/", {"show_my_name": False}),
            ("post", f"challenges/{challenge.pk}/join/", {}),
            ("post", "community/posts/", {"body": "Unsafe", "visibility": "company"}),
            ("post", f"community/posts/{post.pk}/comments/", {"body": "Unsafe"}),
            ("put", f"community/posts/{post.pk}/reactions/like/", {"active": True}),
            ("post", f"community/posts/{post.pk}/report/", {"reason": "Unsafe"})]
        for method, path, data in actions:
            self.assertEqual(getattr(self.client, method)("/app/api/" + path, data, content_type="application/json").status_code, 403)
        self.assertEqual(self.client.post("/app/logout/").status_code, 403)
        self.assertFalse(ChallengeEnrollment.objects.exists())
        self.assertFalse(CommunityComment.objects.exists())
        self.assertEqual(CommunityPost.objects.count(), 1)
        self.assertEqual(self.client.get("/app/logout/").status_code, 405)

    def test_rotation_revocation_deactivation_and_source_switch_invalidate_sessions(self):
        self.login()
        _, replacement = issue_token(self.user)
        self.assertEqual(self.client.get("/app/api/me/").status_code, 401)
        self.assertNotIn(SESSION_KEY, self.client.session)
        self.login(token=replacement)
        revoke_token(self.user)
        self.assertRedirects(self.client.get("/app/"), "/app/login/")
        _, replacement = issue_token(self.user)
        self.login(token=replacement)
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        self.assertEqual(self.client.get("/app/api/me/").status_code, 401)
        self.assertNotIn(SESSION_KEY, self.client.session)
        self.user.is_active = True
        self.user.save(update_fields=["is_active"])
        self.login(token=replacement)
        android = FitnessSource.objects.create(user=self.user, installation_id=uuid4())
        FitnessAccount.objects.filter(user=self.user).update(active_source=android)
        self.assertEqual(self.client.get("/app/api/me/").status_code, 401)
        self.assertContains(self.login(token=replacement), "invalid or no longer active")

    def test_logout_does_not_revoke_healthysync_and_staff_login_is_independent(self):
        staff = User.objects.create_user("staff@example.test", "Staff-password!", is_staff=True, role=Role.ADMIN)
        self.client.force_login(staff)
        staff_id = self.client.session["_auth_user_id"]
        self.login()
        self.assertEqual(self.client.get("/app/api/me/").json()["email"], self.user.email)
        self.assertEqual(self.client.session["_auth_user_id"], staff_id)
        response = self.client.post("/app/logout/", HTTP_X_CSRFTOKEN=self.client.cookies["csrftoken"].value)
        self.assertRedirects(response, "/app/login/")
        self.assertEqual(self.client.session["_auth_user_id"], staff_id)
        self.connection.refresh_from_db()
        self.assertTrue(self.connection.token_hash)
        self.assertEqual(self.client.get("/app/api/me/").status_code, 401)

    def test_token_cannot_grant_staff_access_or_access_existing_jwt_routes(self):
        self.login()
        self.assertEqual(self.client.get("/portal/").status_code, 302)
        self.assertEqual(self.client.get("/admin/").status_code, 302)
        self.assertEqual(self.client.get(reverse("fitness_home")).status_code, 401)
        self.assertEqual(self.client.post(reverse("apple_health_token"), {}, content_type="application/json").status_code, 401)
        staff = User.objects.create_user("admin@example.test", "Staff-password!", role=Role.ADMIN, is_staff=True)
        _, token = issue_token(staff)
        fresh = Client(enforce_csrf_checks=True)
        self.assertContains(self.login(client=fresh, token=token), "invalid or no longer active")
        self.assertNotIn(SESSION_KEY, fresh.session)
        self.assertEqual(self.write("home/").status_code, 405)

    def test_browser_join_and_healthysync_upload_use_shared_progress_and_rankings(self):
        self.login()
        challenge = self.challenge()
        self.assertEqual(self.write(f"challenges/{challenge.pk}/join/").status_code, 201)
        day = self.today.isoformat()
        payload = {"schema_version": 2, "device_id": "real-iphone", "export_id": str(uuid4()),
            "generated_at": timezone.now().isoformat(), "timezone": "UTC", "source": "ios-healthkit",
            "date_range": {"start": f"{day}T00:00:00Z", "end": f"{day}T23:59:59Z"},
            "metrics": [{"id": "healthkit:web-test", "type": "step_count", "value": 1500, "unit": "count",
                "start_at": f"{day}T00:00:00Z", "end_at": timezone.now().isoformat(), "source_name": "iPhone", "metadata": {}}],
            "workouts": [], "deletions": []}
        response = Client().post(reverse("apple_health_sync"), payload, content_type="application/json",
            HTTP_AUTHORIZATION="Bearer " + self.token)
        self.assertEqual(response.status_code, 200, response.content)
        mine = self.client.get("/app/api/challenges/mine/").json()
        self.assertTrue(mine["challenges"][0]["complete"])
        self.assertEqual(mine["points_total"], 50)
        self.assertEqual(mine["badges"][0]["title"], "First win")
        self.assertEqual(self.client.get("/app/api/home/").json()["metrics"]["steps"], 1500)
        self.assertEqual(self.client.get("/app/api/steps/").json()["steps"], 1500)
        ranks = self.client.get("/app/api/rankings/?board=steps_today").json()
        self.assertEqual(ranks["me"]["value"], 1500)
        self.assertEqual(self.write(f"challenges/{challenge.pk}/join/").status_code, 200)
        self.assertEqual(ChallengeEnrollment.objects.count(), 1)
        self.assertEqual(PointsLedger.objects.count(), 1)
        self.assertEqual(BadgeAward.objects.count(), 1)

    def test_challenge_publication_deadline_and_caller_identity(self):
        self.login()
        available = self.challenge()
        draft = self.challenge(is_published=False)
        expired = self.challenge(start_date=self.today - timedelta(days=3), end_date=self.today - timedelta(days=1))
        self.assertEqual([c["id"] for c in self.client.get("/app/api/challenges/").json()], [available.pk])
        self.assertEqual(self.write(f"challenges/{draft.pk}/join/").status_code, 404)
        self.assertEqual(self.write(f"challenges/{expired.pk}/join/").status_code, 400)
        self.assertEqual(self.write(f"challenges/{available.pk}/join/", {"user_id": self.other.pk}).status_code, 201)
        self.assertEqual(ChallengeEnrollment.objects.get().user, self.user)

    def test_community_visibility_pagination_moderation_and_idempotent_posting(self):
        self.login()
        secret = CommunityPost.objects.create(author=self.other, body="Other team only", team=self.other.profile.team, visibility="team")
        for index in range(21):
            CommunityPost.objects.create(author=self.other, body=f"Company {index}", visibility="company")
        feed = self.client.get("/app/api/community/posts/").json()
        self.assertEqual(feed["count"], 21)
        self.assertEqual(len(feed["results"]), 20)
        self.assertIn("/app/api/community/posts/?page=2", feed["next"])
        self.assertEqual(len(self.client.get(feed["next"]).json()["results"]), 1)
        self.assertEqual(self.client.get(f"/app/api/community/posts/{secret.pk}/").status_code, 404)
        body = {"body": "A team walk", "visibility": "team", "hashtags": ["TeamWalk"],
            "client_request_id": str(uuid4()), "author_id": self.other.pk}
        with patch("community.moderation.screen", return_value=("review", "Review requested")):
            response = self.write("community/posts/", body)
        self.assertEqual(response.status_code, 201)
        post = CommunityPost.objects.get(pk=response.json()["id"])
        self.assertEqual((post.author, post.team, post.status), (self.user, self.team, "review"))
        self.assertEqual(self.write("community/posts/", body).status_code, 200)
        self.assertEqual(CommunityPost.objects.filter(author=self.user).count(), 1)
        self.assertIn(post.pk, [p["id"] for p in self.client.get("/app/api/community/posts/").json()["results"]])
        _, token = issue_token(self.other)
        other_client = Client(enforce_csrf_checks=True)
        self.login(other_client, token)
        self.assertEqual(other_client.get(f"/app/api/community/posts/{post.pk}/").status_code, 404)

    def test_comments_reactions_reports_and_author_only_removal(self):
        self.login()
        post = CommunityPost.objects.create(author=self.other, body="Great walk!", visibility="company")
        path = f"community/posts/{post.pk}/"
        self.assertEqual(self.write(path + "comments/", {"body": "Well done!", "author_id": self.other.pk}).status_code, 201)
        self.assertEqual(CommunityComment.objects.get().author, self.user)
        self.assertEqual(len(self.client.get("/app/api/" + path + "comments/").json()), 1)
        for kind in ("like", "celebrate", "fire"):
            self.assertEqual(self.write(path + f"reactions/{kind}/", {"active": True}, "put").status_code, 200)
        self.assertEqual(CommunityReaction.objects.count(), 3)
        self.write(path + "reactions/like/", {"active": False}, "put")
        self.assertEqual(CommunityReaction.objects.count(), 2)
        self.assertEqual(self.write(path + "report/", {"reason": "Please review"}).status_code, 201)
        self.assertEqual(CommunityReport.objects.get().reporter, self.user)
        self.assertEqual(self.write(path, method="delete").status_code, 403)
        own = CommunityPost.objects.create(author=self.user, body="My post", visibility="company")
        self.assertEqual(self.write(f"community/posts/{own.pk}/", method="delete").status_code, 204)
        own.refresh_from_db()
        self.assertEqual(own.status, "hidden")

    def test_team_photo_upload_and_read_are_authenticated_and_scoped(self):
        self.login()
        content = io.BytesIO()
        Image.new("RGB", (20, 20), "red").save(content, format="PNG")
        upload = SimpleUploadedFile("walk.png", content.getvalue(), content_type="image/png")
        result = self.client.post("/app/api/community/posts/photos/", {"image": upload, "body": "Our walk",
            "visibility": "team", "hashtags": ["walk", "team"], "client_request_id": str(uuid4())},
            HTTP_X_CSRFTOKEN=self.client.cookies["csrftoken"].value)
        self.assertEqual(result.status_code, 201, result.content)
        path = f"/app/api/community/posts/{result.json()['id']}/photo/"
        photo = self.client.get(path)
        self.assertEqual(photo.status_code, 200)
        self.assertEqual(photo["Content-Type"], "image/jpeg")
        self.assertIn("no-store", photo["Cache-Control"])
        self.assertTrue(b"".join(photo.streaming_content))
        self.assertEqual(Client().get(path).status_code, 401)
        _, token = issue_token(self.other)
        other_client = Client(enforce_csrf_checks=True)
        self.login(other_client, token)
        self.assertEqual(other_client.get(path).status_code, 404)

    def test_rankings_match_android_preserve_privacy_and_health_is_user_scoped(self):
        self.login()
        self.other.profile.show_in_rankings = False
        self.other.profile.save()
        connection, _ = issue_token(self.other)
        DailyActivity.objects.create(user=self.other, source=connection.source, local_date=self.today,
            time_zone="UTC", observed_at=timezone.now(), steps=9876, verified_metrics={"steps": 9000})
        android = Client()
        for board in ("steps_today", "steps_week", "steps_month", "points", "teams"):
            web = self.client.get(f"/app/api/rankings/?board={board}").json()
            native = android.get(f"/api/v1/challenges/rankings/?board={board}",
                HTTP_AUTHORIZATION="Bearer " + str(AccessToken.for_user(self.user))).json()
            self.assertEqual(web["rows"], native["rows"])
        ranks = self.client.get("/app/api/rankings/?board=steps_today").json()
        self.assertEqual(ranks["rows"][0]["name"], "Colleague")
        self.assertEqual(ranks["rows"][0]["value"], 9000)
        self.assertIsNone(self.client.get("/app/api/home/?user_id=" + str(self.other.pk)).json()["metrics"]["steps"])
        self.assertEqual(self.write("rankings/", {"show_my_name": False}).status_code, 200)
        self.user.profile.refresh_from_db()
        self.assertFalse(self.user.profile.show_in_rankings)
        self.assertEqual(self.client.get("/app/api/rankings/?board=invalid").status_code, 400)

    def test_portal_token_instructions_include_employee_app_and_login_works(self):
        staff = User.objects.create_user("hr@example.test", "Staff-password!", role=Role.HR_ADMIN, is_staff=True)
        staff_client = Client()
        staff_client.force_login(staff)
        response = staff_client.post(reverse("portal:employee_apple_health", args=[self.user.pk]),
            {"action": "generate", "device_label": "Work iPhone"}, secure=True)
        self.assertContains(response, "https://testserver/app/")
        self.assertContains(response, "Paste the same Auth token")
        self.assertRedirects(self.login(token=response.context["token"]), "/app/")
