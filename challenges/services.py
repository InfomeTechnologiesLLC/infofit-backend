from django.db import transaction
from django.utils import timezone

from fitness.models import FitnessAccount
from . import scoring
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
            source = account.active_source if account else None
            result = scoring.score(challenge, enrollment, source)
            complete = result.has_data and result.value >= challenge.target > 0
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
            # The older steps/running fields stay filled for older app versions.
            progress.verified_steps = int(result.value) if challenge.kind == Challenge.Kind.STEPS else 0
            progress.has_step_data = result.has_data
            progress.verified_running_minutes = int(result.value) if challenge.kind == Challenge.Kind.RUNNING else 0
            progress.progress_value = result.value
            progress.flagged_days = result.flagged_days
            progress.is_complete = complete
            progress.evaluated_at = timezone.now()
            progress.save()
