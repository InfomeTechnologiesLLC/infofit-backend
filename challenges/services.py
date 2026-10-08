from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from fitness.models import DailyActivity, ExerciseRecord, FitnessAccount
from .models import BadgeAward, Challenge, ChallengeEnrollment, ChallengeProgress, PointsLedger
from notifications.models import Notification
from notifications.services import create_notification


def reconcile_user(user):
    """Call in the same transaction as a fitness sync or source switch."""
    with transaction.atomic():
        account = FitnessAccount.objects.select_for_update().filter(user=user).first()
        enrollments = ChallengeEnrollment.objects.filter(user=user).select_related("challenge").order_by("pk")
        for enrollment in enrollments:
            challenge = enrollment.challenge
            progress, _ = ChallengeProgress.objects.get_or_create(enrollment=enrollment)
            progress = ChallengeProgress.objects.select_for_update().get(pk=progress.pk)
            steps = 0
            has_data = False
            running_minutes = 0
            if account and account.active_source:
                if challenge.kind == Challenge.Kind.RUNNING:
                    # Session start is precise enough to exclude runs before joining.
                    sessions = ExerciseRecord.objects.filter(
                        user=user, source=account.active_source,
                        local_date__gte=challenge.start_date, local_date__lte=challenge.end_date,
                        start_at__gte=enrollment.joined_at,
                        exercise_type__in=("running", "running_treadmill", "56", "57"),
                    )
                    seconds = sum(max(0, int((row.end_at - row.start_at).total_seconds())) for row in sessions)
                    running_minutes = seconds // 60
                else:
                    # A daily total cannot separate steps before and after joining.
                    # Start with the first full local day after enrollment.
                    rows = DailyActivity.objects.filter(
                        user=user, source=account.active_source,
                        local_date__gt=enrollment.joined_local_date,
                        local_date__gte=challenge.start_date,
                        local_date__lte=challenge.end_date, metric_status__steps="available",
                        steps__isnull=False,
                    )
                    aggregate = rows.aggregate(steps=Sum("steps"))
                    has_data = aggregate["steps"] is not None
                    steps = aggregate["steps"] or 0
            complete = (running_minutes >= challenge.target_running_minutes if challenge.kind == Challenge.Kind.RUNNING
                        else has_data and steps >= challenge.target_steps)
            if complete != progress.is_complete:
                progress.revision += 1
                PointsLedger.objects.create(
                    enrollment=enrollment, user=user, revision=progress.revision,
                    delta=challenge.reward_points if complete else -challenge.reward_points,
                    reason="award" if complete else "correction",
                )
                if challenge.badge_title:
                    badge, _ = BadgeAward.objects.get_or_create(
                        enrollment=enrollment, defaults={"awarded_at": timezone.now()}
                    )
                    badge.is_active = complete
                    badge.revoked_at = None if complete else timezone.now()
                    badge.save(update_fields=["is_active", "revoked_at"])
                if complete:
                    create_notification(
                        user, Notification.Kind.CHALLENGE_COMPLETE, "Challenge complete",
                        f"You completed {challenge.title}.",
                        f"challenge:{enrollment.pk}:complete:{progress.revision}",
                    )
                    if challenge.badge_title:
                        create_notification(
                            user, Notification.Kind.BADGE_EARNED, "Badge earned",
                            f"You earned {challenge.badge_title}.",
                            f"challenge:{enrollment.pk}:badge:{progress.revision}",
                        )
            progress.verified_steps = steps
            progress.has_step_data = has_data
            progress.verified_running_minutes = running_minutes
            progress.is_complete = complete
            progress.evaluated_at = timezone.now()
            progress.save()
