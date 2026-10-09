from datetime import timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from celery import shared_task
from django.utils import timezone

from notifications.models import Notification
from notifications.services import create_notification
from .models import HydrationLog, ReminderSettings


@shared_task
def create_wellness_reminders():
    """Create opt-in timed prompts. A lack of steps never proves someone is sitting."""
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
        except ZoneInfoNotFoundError:
            continue
        elapsed = (local.hour - settings.start_hour) * 60 + local.minute
        if local.hour < settings.start_hour or local.hour >= settings.end_hour:
            continue
        date_key = local.date().isoformat()
        if settings.water_enabled and 30 <= settings.water_interval_minutes <= 240:
            interval = settings.water_interval_minutes
            slot = elapsed // interval
            if slot >= 1 and elapsed % interval < 5 and not HydrationLog.objects.filter(
                user=settings.user, created_at__gte=now - timedelta(minutes=interval)
            ).exists():
                _, created = create_notification(
                    settings.user, Notification.Kind.WATER_BREAK,
                    "IVA water break", "If it suits you, take a moment to drink water. Log it in Home if you wish.",
                    f"wellness:water:{date_key}:{slot}",
                )
                created_count += int(created)
        if settings.movement_enabled and 30 <= settings.movement_interval_minutes <= 240:
            interval = settings.movement_interval_minutes
            slot = elapsed // interval
            if slot >= 1 and elapsed % interval < 5:
                _, created = create_notification(
                    settings.user, Notification.Kind.MOVEMENT_BREAK,
                    "IVA movement break", "If you can, consider a short walk or stretch break.",
                    f"wellness:movement:{date_key}:{slot}",
                )
                created_count += int(created)
    return created_count
