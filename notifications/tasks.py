from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from .models import PushDelivery


# Kinds whose text is safe to show in full on the phone. Others (BMI, HR figures) only say "new notification".
# The app also marks every notification private, so a locked screen shows only "INFOME FIT" until unlocked.
DETAILED_KINDS = {"water_break", "movement_break", "challenge_reminder", "challenge_alert", "challenge_complete",
                  "badge_earned", "meal_prompt", "goal_nudge", "coach_nudge", "weekly_review", "reengagement",
                  "community_reply"}


def push_message(token, notification):
    """A data-only, high-priority message: the app builds the notification itself, with action buttons."""
    from firebase_admin import messaging

    detailed = notification.kind in DETAILED_KINDS
    data = {"notification_id": str(notification.pk), "kind": notification.kind,
            "title": notification.title if detailed else "INFOME FIT",
            "body": notification.body if detailed else "You have a new notification"}
    for key, value in (notification.data or {}).items() if detailed else ():
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            data[f"x_{key}"] = str(value)[:100]
    return messaging.Message(token=token, data=data, android=messaging.AndroidConfig(priority="high"))


@shared_task
def deliver_pending_pushes():
    """Best-effort FCM delivery. Inbox creation never depends on Redis or Firebase."""
    if not settings.FIREBASE_CREDENTIALS_FILE:
        return 0
    import firebase_admin
    from firebase_admin import credentials, messaging

    try:
        firebase_admin.get_app()
    except ValueError:
        firebase_admin.initialize_app(credentials.Certificate(settings.FIREBASE_CREDENTIALS_FILE))
    due = PushDelivery.objects.filter(state__in=[PushDelivery.State.PENDING, PushDelivery.State.RETRY]).filter(
        Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=timezone.now())
    ).select_related("notification", "device").order_by("pk")[:100]
    sent = 0
    for delivery in due:
        if not delivery.device.enabled or delivery.device.user_id != delivery.notification.user_id:
            delivery.state = PushDelivery.State.INVALID
            delivery.save(update_fields=["state"])
            continue
        try:
            messaging.send(push_message(delivery.device.token, delivery.notification))
        except messaging.UnregisteredError:
            delivery.state = PushDelivery.State.INVALID
        except Exception:
            delivery.attempts += 1
            delivery.state = PushDelivery.State.RETRY
            delivery.next_attempt_at = timezone.now() + timedelta(minutes=min(60, 2 ** min(delivery.attempts, 6)))
        else:
            delivery.state = PushDelivery.State.SENT
            delivery.sent_at = timezone.now()
            delivery.next_attempt_at = None
            sent += 1
        delivery.save(update_fields=["state", "attempts", "next_attempt_at", "sent_at"])
    return sent
