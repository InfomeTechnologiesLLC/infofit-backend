from datetime import timedelta
try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # Python 3.8 hosts
    from backports.zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from celery import shared_task
from django.utils import timezone

from notifications.models import Notification
from notifications.services import create_notification
from .models import BodyMeasurement, HydrationLog, ReminderSettings


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


@shared_task
def create_weekly_bmi_reminders():
    """Opt-in weekly inbox prompt to update BMI, at the employee's reminder start time.

    Sent only when the latest measurement is 7+ days old (or none exists); one per ISO week.
    """
    now = timezone.now()
    created_count = 0
    for settings in ReminderSettings.objects.filter(bmi_enabled=True).select_related("user").iterator():
        if not settings.user.is_active:
            continue
        try:
            local = now.astimezone(ZoneInfo(settings.time_zone))
        except (ZoneInfoNotFoundError, ValueError):
            continue
        minute_of_day = local.hour * 60 + local.minute
        if not (settings.start_minute <= minute_of_day < settings.start_minute + 5):
            continue
        latest = BodyMeasurement.objects.filter(user=settings.user).order_by("-local_date").values_list(
            "local_date", flat=True).first()
        if latest and (local.date() - latest).days < 7:
            continue
        year, week, _ = local.date().isocalendar()
        _, created = create_notification(
            settings.user, Notification.Kind.BMI_CHECKIN,
            "Weekly BMI check-in", "If you wish, update your weight to keep your weekly progress up to date.",
            f"wellness:bmi:{year}-W{week}", push=False,
        )
        created_count += int(created)
    return created_count
