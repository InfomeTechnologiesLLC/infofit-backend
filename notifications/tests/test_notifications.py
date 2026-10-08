from uuid import uuid4

from django.test import override_settings
from rest_framework.test import APITestCase

from accounts.models import User
from community.models import CommunityPost
from notifications.models import Notification, PushDelivery, PushDevice
from notifications.services import create_notification
from notifications.tasks import deliver_pending_pushes


class NotificationTests(APITestCase):
    def setUp(self):
        self.alice = User.objects.create_user(email="alice@phase8.test", password="Long-password-123")
        self.bob = User.objects.create_user(email="bob@phase8.test", password="Long-password-123")
        self.client.force_authenticate(self.alice)

    def test_inbox_is_private_and_read_is_idempotent(self):
        own, _ = create_notification(self.alice, Notification.Kind.BADGE_EARNED,
                                     "Badge earned", "You earned a badge.", "badge:1")
        create_notification(self.bob, Notification.Kind.BADGE_EARNED,
                            "Other", "Private", "badge:2")
        page = self.client.get("/api/v1/notifications/")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.data["unread_count"], 1)
        self.assertEqual([row["id"] for row in page.data["results"]], [own.id])
        url = f"/api/v1/notifications/{own.id}/read/"
        self.assertEqual(self.client.post(url).status_code, 200)
        self.assertEqual(self.client.post(url).status_code, 200)
        self.assertEqual(self.client.get("/api/v1/notifications/").data["unread_count"], 0)
        self.client.force_authenticate(self.bob)
        self.assertEqual(self.client.post(url).status_code, 404)
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get("/api/v1/notifications/").status_code, 401)

    def test_event_and_push_attempts_are_idempotent_and_device_switch_is_safe(self):
        token = "token-" + "a" * 60
        self.assertEqual(self.client.post("/api/v1/notifications/devices/", {"token": token}).status_code, 201)
        first, created = create_notification(self.alice, Notification.Kind.CHALLENGE_COMPLETE,
                                             "Complete", "Done", "challenge:1:revision:1")
        second, again = create_notification(self.alice, Notification.Kind.CHALLENGE_COMPLETE,
                                            "Complete", "Done", "challenge:1:revision:1")
        self.assertTrue(created)
        self.assertFalse(again)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(PushDelivery.objects.count(), 1)
        self.client.force_authenticate(self.bob)
        self.assertEqual(self.client.post("/api/v1/notifications/devices/", {"token": token}).status_code, 201)
        self.assertEqual(PushDevice.objects.get(token=token).user_id, self.bob.id)
        self.assertEqual(PushDelivery.objects.count(), 0)
        self.assertEqual(self.client.delete("/api/v1/notifications/devices/", {"token": token}, format="json").status_code, 204)
        self.assertFalse(PushDevice.objects.exists())

    def test_reply_notifies_only_post_author_and_not_self(self):
        post = CommunityPost.objects.create(author=self.alice, visibility="company", body="Hello")
        self.client.force_authenticate(self.bob)
        self.assertEqual(self.client.post(f"/api/v1/community/posts/{post.pk}/comments/", {"body": "Hi"}).status_code, 201)
        self.assertEqual(Notification.objects.filter(user=self.alice, kind="community_reply").count(), 1)
        self.assertEqual(Notification.objects.filter(user=self.bob).count(), 0)
        self.client.force_authenticate(self.alice)
        self.assertEqual(self.client.post(f"/api/v1/community/posts/{post.pk}/comments/", {"body": "Thanks"}).status_code, 201)
        self.assertEqual(Notification.objects.filter(user=self.alice).count(), 1)

    def test_post_retry_key_prevents_duplicates_and_rejects_changed_payload(self):
        key = str(uuid4())
        body = {"body": "An offline post", "visibility": "company", "client_request_id": key}
        first = self.client.post("/api/v1/community/posts/", body, format="json")
        self.assertEqual(first.status_code, 201)
        self.assertEqual(self.client.post("/api/v1/community/posts/", body, format="json").status_code, 200)
        self.assertEqual(CommunityPost.objects.filter(author=self.alice).count(), 1)
        self.assertEqual(self.client.post("/api/v1/community/posts/", {**body, "body": "Changed"}, format="json").status_code, 409)

    @override_settings(FIREBASE_CREDENTIALS_FILE="")
    def test_no_firebase_config_keeps_inbox_without_claiming_push(self):
        PushDevice.objects.create(user=self.alice, token="token-" + "a" * 60)
        create_notification(self.alice, Notification.Kind.BADGE_EARNED, "Badge", "Earned", "badge:3")
        self.assertEqual(deliver_pending_pushes(), 0)
        self.assertEqual(PushDelivery.objects.get().state, PushDelivery.State.PENDING)
