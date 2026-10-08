import io
import tempfile
import uuid

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from PIL import Image
from rest_framework.test import APITestCase
from django.test import TestCase

from accounts.models import EmployeeProfile, Role, Team, User
from community.models import CommunityComment, CommunityPost, CommunityReaction, CommunityReport


class CommunityApiTests(APITestCase):
    def setUp(self):
        self.team_a = Team.objects.create(name="Team A")
        self.team_b = Team.objects.create(name="Team B")
        self.alice = User.objects.create_user(email="alice@example.test", password="Strong-Passphrase-123", first_name="Alice")
        self.bob = User.objects.create_user(email="bob@example.test", password="Strong-Passphrase-123", first_name="Bob")
        self.carol = User.objects.create_user(email="carol@example.test", password="Strong-Passphrase-123", first_name="Carol")
        EmployeeProfile.objects.filter(user=self.alice).update(team=self.team_a)
        EmployeeProfile.objects.filter(user=self.bob).update(team=self.team_a)
        EmployeeProfile.objects.filter(user=self.carol).update(team=self.team_b)
        self.client.force_authenticate(self.alice)

    def create_post(self, visibility="team"):
        return self.client.post("/api/v1/community/posts/", {
            "body": "A real walk today", "hashtags": ["#walking"], "visibility": visibility
        }, format="json")

    def test_empty_feed_auth_and_team_isolation(self):
        self.assertEqual(self.client.get("/api/v1/community/posts/").data["results"], [])
        team_post = self.create_post()
        self.assertEqual(team_post.status_code, 201, team_post.data)
        self.assertEqual(team_post.data["likes_count"], 0)
        self.client.force_authenticate(self.bob)
        self.assertEqual(len(self.client.get("/api/v1/community/posts/").data["results"]), 1)
        self.client.force_authenticate(self.carol)
        self.assertEqual(self.client.get("/api/v1/community/posts/").data["results"], [])
        self.assertEqual(self.client.get(f"/api/v1/community/posts/{team_post.data['id']}/").status_code, 404)
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get("/api/v1/community/posts/").status_code, 401)

    def test_company_post_and_author_controls(self):
        response = self.create_post("company")
        pk = response.data["id"]
        self.client.force_authenticate(self.carol)
        self.assertEqual(len(self.client.get("/api/v1/community/posts/").data["results"]), 1)
        self.assertEqual(self.client.patch(f"/api/v1/community/posts/{pk}/", {"body": "Changed"}, format="json").status_code, 403)
        self.assertEqual(self.client.delete(f"/api/v1/community/posts/{pk}/").status_code, 403)
        self.client.force_authenticate(self.alice)
        self.assertEqual(self.client.patch(f"/api/v1/community/posts/{pk}/", {"body": "Updated"}, format="json").data["body"], "Updated")
        self.assertEqual(self.client.delete(f"/api/v1/community/posts/{pk}/").status_code, 204)
        self.assertEqual(self.client.get("/api/v1/community/posts/").data["results"], [])
        self.assertEqual(CommunityPost.objects.get(pk=pk).status, "hidden")

    def test_reactions_are_idempotent_and_comments_are_scoped(self):
        pk = self.create_post().data["id"]
        url = f"/api/v1/community/posts/{pk}/reactions/like/"
        self.assertEqual(self.client.put(url, {"active": True}, format="json").data["likes_count"], 1)
        self.assertEqual(self.client.put(url, {"active": True}, format="json").data["likes_count"], 1)
        self.assertEqual(CommunityReaction.objects.count(), 1)
        self.assertEqual(self.client.put(url, {"active": False}, format="json").data["likes_count"], 0)
        comment_url = f"/api/v1/community/posts/{pk}/comments/"
        comment = self.client.post(comment_url, {"body": "Great job"}, format="json")
        self.assertEqual(comment.status_code, 201)
        self.client.force_authenticate(self.bob)
        self.assertEqual(self.client.get(comment_url).data[0]["body"], "Great job")
        self.assertEqual(self.client.delete(f"{comment_url}{comment.data['id']}/").status_code, 403)
        self.client.force_authenticate(self.carol)
        self.assertEqual(self.client.get(comment_url).status_code, 404)
        self.assertEqual(self.client.post(comment_url, {"body": "Unseen"}, format="json").status_code, 404)
        self.client.force_authenticate(self.alice)
        self.assertEqual(self.client.delete(f"{comment_url}{comment.data['id']}/").status_code, 204)
        self.assertEqual(CommunityComment.objects.get().status, "hidden")

    def test_report_is_recorded_without_fake_moderation_claim(self):
        pk = self.create_post().data["id"]
        self.client.force_authenticate(self.bob)
        url = f"/api/v1/community/posts/{pk}/report/"
        self.assertEqual(self.client.post(url, {"reason": "Inappropriate"}, format="json").status_code, 201)
        self.assertEqual(self.client.post(url, {"reason": "Repeated"}, format="json").status_code, 200)
        self.assertEqual(CommunityReport.objects.count(), 1)
        self.assertEqual(CommunityReport.objects.get().status, "open")
        self.client.force_authenticate(self.carol)
        self.assertEqual(self.client.post(url, {"reason": "Can't see"}, format="json").status_code, 404)

    def test_teamless_user_cannot_create_team_post_and_admin_login_renders(self):
        EmployeeProfile.objects.filter(user=self.alice).update(team=None)
        self.assertEqual(self.create_post().status_code, 400)
        self.assertEqual(self.create_post("company").status_code, 201)
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get("/admin/login/").status_code, 200)


class CommunityPhotoTests(APITestCase):
    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        setting = override_settings(MEDIA_ROOT=self.media.name)
        setting.enable()
        self.addCleanup(setting.disable)
        team = Team.objects.create(name="Photo team")
        self.author = User.objects.create_user(email="photo@example.test", password="Strong-Passphrase-123")
        self.colleague = User.objects.create_user(email="colleague@example.test", password="Strong-Passphrase-123")
        self.other = User.objects.create_user(email="other@example.test", password="Strong-Passphrase-123")
        EmployeeProfile.objects.filter(user__in=[self.author, self.colleague]).update(team=team)
        self.client.force_authenticate(self.author)

    def photo(self, color="red"):
        output = io.BytesIO()
        Image.new("RGB", (20, 20), color).save(output, format="PNG")
        return SimpleUploadedFile("sample.png", output.getvalue(), content_type="image/png")

    def test_photo_post_is_private_and_replay_is_idempotent(self):
        request_id = str(uuid.uuid4())
        url = "/api/v1/community/posts/photos/"
        first = self.client.post(url, {"body": "", "visibility": "team", "hashtags": ["walking"],
                                       "client_request_id": request_id, "image": self.photo()}, format="multipart")
        self.assertEqual(first.status_code, 201, first.data)
        self.assertTrue(first.data["has_image"])
        self.assertEqual(first.data["hashtags"], ["#walking"])
        pk = first.data["id"]
        replay = self.client.post(url, {"body": "", "visibility": "team", "hashtags": ["walking"],
                                        "client_request_id": request_id, "image": self.photo()}, format="multipart")
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["id"], pk)
        self.assertEqual(CommunityPost.objects.count(), 1)
        self.assertEqual(self.client.post(url, {"body": "", "visibility": "team", "hashtags": ["walking"],
                                                "client_request_id": request_id, "image": self.photo("blue")},
                                          format="multipart").status_code, 409)
        self.assertEqual(self.client.post("/api/v1/community/posts/", {"body": "Text", "visibility": "team",
                                           "client_request_id": request_id}, format="json").status_code, 409)
        self.client.force_authenticate(self.colleague)
        image = self.client.get(f"/api/v1/community/posts/{pk}/photo/")
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image["Content-Type"], "image/jpeg")
        self.assertEqual(image["Cache-Control"], "private, no-store")
        image.close()
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get(f"/api/v1/community/posts/{pk}/photo/").status_code, 404)
        self.client.force_authenticate(self.author)
        self.assertEqual(self.client.delete(f"/api/v1/community/posts/{pk}/").status_code, 204)
        self.assertEqual(self.client.get(f"/api/v1/community/posts/{pk}/photo/").status_code, 404)
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get(f"/api/v1/community/posts/{pk}/photo/").status_code, 401)

    def test_invalid_and_oversized_files_are_rejected(self):
        url = "/api/v1/community/posts/photos/"
        self.assertEqual(self.client.post(url, {"body": "test", "visibility": "company",
                                                "image": SimpleUploadedFile("fake.jpg", b"not an image")},
                                          format="multipart").status_code, 400)
        self.assertEqual(self.client.post(url, {"body": "test", "visibility": "company",
                                                "image": SimpleUploadedFile("large.jpg", b"x" * (5 * 1024 * 1024 + 1))},
                                          format="multipart").status_code, 400)
        self.assertEqual(CommunityPost.objects.count(), 0)


class CommunityReportAdminTests(TestCase):
    def test_report_shows_post_and_reason_and_admin_can_hide_it(self):
        admin_user = User.objects.create_user(
            email="admin@example.test", password="Strong-Passphrase-123", role=Role.ADMIN, is_staff=True
        )
        author = User.objects.create_user(email="author@example.test", password="Strong-Passphrase-123")
        reporter = User.objects.create_user(email="reporter@example.test", password="Strong-Passphrase-123")
        post = CommunityPost.objects.create(
            author=author, body="Specific reported post text", visibility=CommunityPost.Visibility.COMPANY
        )
        report = CommunityReport.objects.create(post=post, reporter=reporter, reason="Harassing language")
        self.client.force_login(admin_user)
        url = "/admin/community/communityreport/"
        page = self.client.get(url)
        self.assertContains(page, "Specific reported post text")
        self.assertContains(page, "Harassing language")
        self.assertContains(page, "author@example.test")
        self.assertContains(page, f"/admin/community/communitypost/{post.pk}/change/")
        self.assertContains(self.client.get(f"{url}{report.pk}/change/"), "Specific reported post text")
        response = self.client.post(url, {
            "action": "hide_reported_posts", "_selected_action": [str(report.pk)], "index": "0"
        })
        self.assertEqual(response.status_code, 302)
        post.refresh_from_db()
        report.refresh_from_db()
        self.assertEqual(post.status, CommunityPost.Status.HIDDEN)
        self.assertEqual(report.status, CommunityReport.Status.RESOLVED)


class CommunityPhotoAdminTests(TestCase):
    def test_admin_can_review_photo_without_public_media_access(self):
        media = tempfile.TemporaryDirectory()
        self.addCleanup(media.cleanup)
        setting = override_settings(MEDIA_ROOT=media.name)
        setting.enable()
        self.addCleanup(setting.disable)
        admin_user = User.objects.create_user("photo-admin@example.test", "Strong-passphrase-123",
                                              role=Role.ADMIN, is_staff=True)
        author = User.objects.create_user("photo-author@example.test", "Strong-passphrase-123")
        post = CommunityPost.objects.create(author=author, body="", visibility="company")
        output = io.BytesIO()
        Image.new("RGB", (20, 20), "green").save(output, format="JPEG")
        post.image.save("review.jpg", SimpleUploadedFile("review.jpg", output.getvalue()), save=True)
        report = CommunityReport.objects.create(post=post, reporter=admin_user, reason="Review this photo")
        self.client.force_login(admin_user)
        report_page = self.client.get(f"/admin/community/communityreport/{report.pk}/change/")
        self.assertContains(report_page, "View photo")
        photo = self.client.get(f"/admin/community/communitypost/{post.pk}/photo/")
        self.assertEqual(photo.status_code, 200)
        photo.close()
        self.client.logout()
        self.assertEqual(self.client.get(f"/admin/community/communitypost/{post.pk}/photo/").status_code, 302)
