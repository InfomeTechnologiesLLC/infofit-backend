"""A daily IVA nudge for enrolled, unfinished challenges."""
try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # Python 3.8 hosts
    from backports.zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from celery import shared_task
from django.utils import timezone

from fitness.models import FitnessAccount
from notifications.models import Notification
from notifications.services import create_notification
from wellness.models import ReminderSettings

from .models import ChallengeEnrollment


@shared_task
def create_challenge_reminders():
    now = timezone.now()
    count = 0
    enrollments = ChallengeEnrollment.objects.filter(
        user__is_active=True, challenge__is_published=True,
    ).select_related("challenge", "user", "progress").iterator()
    for enrollment in enrollments:
        # Opt-in only, at the time the employee chose in IVA reminder settings.
        reminder = ReminderSettings.objects.filter(user=enrollment.user, challenge_enabled=True).first()
        if not reminder:
            continue
        account = FitnessAccount.objects.filter(user=enrollment.user).first()
        try:
            local_now = now.astimezone(ZoneInfo(account.time_zone if account else "UTC"))
        except (ZoneInfoNotFoundError, ValueError):
            continue
        today = local_now.date()
        if not (enrollment.challenge.start_date <= today <= enrollment.challenge.end_date):
            continue
        if local_now.hour * 60 + local_now.minute < reminder.challenge_minute:
            continue
        progress = getattr(enrollment, "progress", None)
        if progress and progress.is_complete:
            continue
        _, created = create_notification(
            enrollment.user, Notification.Kind.CHALLENGE_REMINDER,
            "IVA challenge reminder",
            f"Your {enrollment.challenge.title} challenge is still in progress. Check your activity when it suits you.",
            f"challenge:reminder:{enrollment.pk}:{today.isoformat()}", push=False,
        )
        count += int(created)
    return count
