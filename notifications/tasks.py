from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from .models import PushDelivery


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
            # Keep details in the authenticated inbox, not on a potentially shared lock screen.
            messaging.send(messaging.Message(
                token=delivery.device.token,
                notification=messaging.Notification(title="INFOME FIT", body="You have a new notification"),
                data={"notification_id": str(delivery.notification_id)},
            ))
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
