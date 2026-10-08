"""Recalculate existing enrollments after activity or scoring rules change."""
from django.core.management.base import BaseCommand

from accounts.models import User
from challenges.models import ChallengeEnrollment
from challenges.services import reconcile_user


class Command(BaseCommand):
    help = "Recalculate all challenge enrollments and apply points corrections."

    def handle(self, *args, **options):
        user_ids = ChallengeEnrollment.objects.values_list("user_id", flat=True).distinct()
        count = 0
        for user in User.objects.filter(pk__in=user_ids).iterator():
            reconcile_user(user)
            count += 1
        self.stdout.write(self.style.SUCCESS(f"Recalculated challenges for {count} users."))
