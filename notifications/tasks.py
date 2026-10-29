import logging
import os
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from .models import PushDelivery, SchedulerHeartbeat

log = logging.getLogger("infome.notifications")

BATCH = 100

# Kinds whose text is safe to show in full on the phone. Others (BMI, HR figures) only say "new notification".
# The app also marks every notification private, so a locked screen shows only "INFOME FIT" until unlocked.
DETAILED_KINDS = {"water_break", "movement_break", "challenge_reminder", "challenge_alert", "challenge_complete",
                  "badge_earned", "meal_prompt", "goal_nudge", "coach_nudge", "weekly_review", "reengagement",
                  "community_reply", "new_challenge", "new_program", "test"}


def push_message(token, notification):
    """A data-only, high-priority message: the app builds the notification itself, with action buttons."""
    from firebase_admin import messaging

    detailed = notification.kind in DETAILED_KINDS
    data = {"notification_id": str(notification.pk), "kind": notification.kind,
            "title": notification.title if detailed else "INFOME FIT",
            "body": notification.body if detailed else "You have a new notification"}
    for key, value in (notification.data or {}).items() if detailed else ():
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            data["x_{}".format(key)] = str(value)[:100]
    return messaging.Message(token=token, data=data, android=messaging.AndroidConfig(priority="high"))


def push_problem():
    """Why pushes cannot be sent right now, or "" when Firebase is set up."""
    path = settings.FIREBASE_CREDENTIALS_FILE
    if not path:
        return "FIREBASE_CREDENTIALS_FILE is not set in the server .env file."
    if not os.path.isfile(path):
        return "The Firebase key file {} does not exist on the server.".format(path)
    try:
        import firebase_admin  # noqa: F401
    except ImportError:
        return "The firebase-admin package is not installed on the server."
    return ""


def _firebase():
    import firebase_admin
    from firebase_admin import credentials, messaging

    try:
        firebase_admin.get_app()
    except ValueError:
        firebase_admin.initialize_app(credentials.Certificate(settings.FIREBASE_CREDENTIALS_FILE))
    return messaging


def _send_one(delivery, messaging):
    """Send one delivery and record the outcome. Returns "" on success, else a short reason."""
    reason = ""
    if not delivery.device.enabled or delivery.device.user_id != delivery.notification.user_id:
        delivery.state = PushDelivery.State.INVALID
        delivery.save(update_fields=["state"])
        return "This phone is no longer signed in."
    try:
        messaging.send(push_message(delivery.device.token, delivery.notification))
    except messaging.UnregisteredError:
        delivery.state = PushDelivery.State.INVALID
        reason = "The phone's push registration has expired (the app was removed or its data cleared)."
    except Exception as error:  # network, bad key, quota: try again later
        delivery.attempts += 1
        delivery.state = PushDelivery.State.RETRY
        delivery.next_attempt_at = timezone.now() + timedelta(minutes=min(60, 2 ** min(delivery.attempts, 6)))
        reason = "Firebase refused the message: {}".format(error)[:300]
        log.warning("Push delivery %s failed: %s", delivery.pk, error)
    else:
        delivery.state = PushDelivery.State.SENT
        delivery.sent_at = timezone.now()
        delivery.next_attempt_at = None
    delivery.save(update_fields=["state", "attempts", "next_attempt_at", "sent_at"])
    return reason


@shared_task
def deliver_pending_pushes(rounds=1):
    """Best-effort FCM delivery. Inbox creation never depends on Redis or Firebase."""
    problem = push_problem()
    if problem:
        SchedulerHeartbeat.beat("push_delivery", ok=False, summary=problem)
        return 0
    messaging = _firebase()
    sent = 0
    for _ in range(max(1, rounds)):
        due = list(PushDelivery.objects.filter(state__in=[PushDelivery.State.PENDING, PushDelivery.State.RETRY]).filter(
            Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=timezone.now())
        ).select_related("notification", "device").order_by("pk")[:BATCH])
        for delivery in due:
            sent += int(_send_one(delivery, messaging) == "")
        if len(due) < BATCH:
            break
    SchedulerHeartbeat.beat("push_delivery", ok=True, summary="{} sent".format(sent))
    return sent


def deliver_now(notification):
    """Send one notification's pushes straight away (portal test button). Returns (sent_count, problems)."""
    problem = push_problem()
    deliveries = list(notification.deliveries.select_related("device", "notification"))
    if problem:
        return 0, [problem]
    if not deliveries:
        return 0, ["This person has no phone registered for notifications. They need to open the app once "
                   "(signed in) on a build that includes google-services.json, and allow notifications."]
    messaging = _firebase()
    reasons = [_send_one(delivery, messaging) for delivery in deliveries]
    return sum(1 for r in reasons if not r), [r for r in reasons if r]
