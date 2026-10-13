from .models import Notification, PushDelivery


def create_notification(user, kind, title, body, event_key, push=True):
    """Create an idempotent inbox event and durable push attempts in the caller's transaction.

    Pass push=False for reminders the phone already shows by itself, to avoid a duplicate alert.
    """
    notification, created = Notification.objects.get_or_create(
        user=user, event_key=event_key,
        defaults={"kind": kind, "title": title[:120], "body": body[:240]},
    )
    if created and push:
        PushDelivery.objects.bulk_create([
            PushDelivery(notification=notification, device=device)
            for device in user.push_devices.filter(enabled=True)
        ], ignore_conflicts=True)
    return notification, created
