from datetime import timedelta
try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # Python 3.8 hosts
    from backports.zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from celery import shared_task
from django.utils import timezone

from notifications.models import Notification
from notifications.services import create_notification
from .models import HydrationLog, ReminderSettings


def _due_slot(minute_of_day, start_minute, end_minute, interval):
    """Slot number when a reminder is due within this 5-minute beat window, else None.

    Matches the phone: first reminder at the start time, then every interval up to the end time.
    """
    if not (15 <= interval <= 360) or minute_of_day < start_minute or minute_of_day > end_minute:
        return None
    elapsed = minute_of_day - start_minute
    return elapsed // interval if elapsed % interval < 5 else None


@shared_task
def create_wellness_reminders():
    """Create opt-in timed inbox prompts. A lack of steps never proves someone is sitting.

    The phone shows these reminders itself, so no push is queued here (push=False).
    """
    now = timezone.now()
    created_count = 0
    settings_rows = ReminderSettings.objects.filter(
        water_enabled=True
    ) | ReminderSettings.objects.filter(movement_enabled=True)
    for settings in settings_rows.select_related("user").distinct().iterator():
        if not settings.user.is_active:
            continue
        try:
            local = now.astimezone(ZoneInfo(settings.time_zone))
        except (ZoneInfoNotFoundError, ValueError):
            continue
        minute_of_day = local.hour * 60 + local.minute
        date_key = local.date().isoformat()
        if settings.water_enabled:
            interval = settings.water_interval_minutes
            slot = _due_slot(minute_of_day, settings.start_minute, settings.end_minute, interval)
            if slot is not None and not HydrationLog.objects.filter(
                user=settings.user, created_at__gte=now - timedelta(minutes=interval)
            ).exists():
                _, created = create_notification(
                    settings.user, Notification.Kind.WATER_BREAK,
                    "IVA water break", "If it suits you, take a moment to drink water. Log it in Home if you wish.",
                    f"wellness:water:{date_key}:{settings.start_minute}:{interval}:{slot}", push=False,
                )
                created_count += int(created)
        if settings.movement_enabled:
            interval = settings.movement_interval_minutes
            slot = _due_slot(minute_of_day, settings.start_minute, settings.end_minute, interval)
            if slot is not None:
                _, created = create_notification(
                    settings.user, Notification.Kind.MOVEMENT_BREAK,
                    "IVA movement break", "If you can, consider a short walk or stretch break.",
                    f"wellness:movement:{date_key}:{settings.start_minute}:{interval}:{slot}", push=False,
                )
                created_count += int(created)
    return created_count
