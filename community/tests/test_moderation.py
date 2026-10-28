from unittest.mock import patch

from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APITestCase

from accounts.models import Role, User
from community.models import CommunityPost

PW = "Strong-passphrase-123"


class ModerationTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.author = User.objects.create_user("author@example.test", PW)
        self.reader = User.objects.create_user("reader@example.test", PW)
        self.client.force_authenticate(self.author)

    def post(self, body):
        return self.client.post("/api/v1/community/posts/", {"body": body, "visibility": "company"}, format="json")

    @override_settings(GEMINI_API_KEY="k")
    @patch("community.moderation.complete_json", return_value={"verdict": "review", "reason": "Insult aimed at a colleague."})
    def test_flagged_post_is_held_and_only_the_author_sees_it(self, _):
        response = self.post("Something unkind")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["status"], "review")
        post = CommunityPost.objects.get()
        self.assertEqual(post.moderation_note, "Insult aimed at a colleague.")
        self.assertEqual(len(self.client.get("/api/v1/community/posts/").data["results"]), 1)
        self.client.force_authenticate(self.reader)
        self.assertEqual(self.client.get("/api/v1/community/posts/").data["results"], [])

        staff = User.objects.create_user("mod@example.test", PW, role=Role.ADMIN, is_staff=True)
        self.client.force_login(staff)
        self.client.post(f"/portal/posts/{post.pk}/approve/")
        post.refresh_from_db()
        self.assertEqual(post.status, "published")

    @override_settings(GEMINI_API_KEY="k")
    @patch("community.moderation.complete_json", return_value={"verdict": "ok", "reason": ""})
    def test_ordinary_post_publishes(self, _):
        self.assertEqual(self.post("Walked 10k today!").data["status"], "published")

    def test_without_ai_contact_details_are_still_held(self):
        self.assertEqual(self.post("Call me on +971 50 123 4567").data["status"], "review")
        self.assertEqual(self.post("Great run this morning").data["status"], "published")

    @override_settings(GEMINI_API_KEY="k")
    @patch("community.moderation.complete_json", return_value={"caption": "Walked 10k today! Who's joining tomorrow?",
                                                              "hashtags": ["#Walking", "team spirit", "10k"]})
    def test_assist_cleans_suggestions(self, _):
        response = self.client.post("/api/v1/community/posts/assist/", {"body": "walked 10k today who joins tmrw"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["hashtags"], ["walking", "10k"])
