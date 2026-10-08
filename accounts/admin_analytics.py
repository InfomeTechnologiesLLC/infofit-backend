"""Operational counts for authorized administrators; no individual health data."""
from datetime import timedelta

from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.shortcuts import render
from django.utils import timezone

from accounts.models import ADMIN_ROLES, Role, Team, User
from challenges.models import Challenge, ChallengeProgress
from community.models import CommunityPost, CommunityReport
from fitness.models import FitnessSource
from notifications.models import PushDelivery
from nutrition.models import Meal
from wellness.models import ProgramCheckIn, WellnessProgram


def analytics(request):
    user = request.user
    if not (user.is_authenticated and user.is_active and user.is_staff and
            (user.is_superuser or getattr(user, "role", None) in ADMIN_ROLES)):
        raise PermissionDenied

    recent = timezone.now() - timedelta(days=7)
    cards = [
        ("Active employees", User.objects.filter(role=Role.EMPLOYEE, is_active=True).count()),
        ("Teams", Team.objects.count()),
        ("Sources synced in 7 days", FitnessSource.objects.filter(last_successful_sync_at__gte=recent).count()),
        ("Published challenges", Challenge.objects.filter(is_published=True).count()),
        ("Completed enrollments", ChallengeProgress.objects.filter(is_complete=True).count()),
        ("Published posts", CommunityPost.objects.filter(status=CommunityPost.Status.PUBLISHED).count()),
        ("Saved meals", Meal.objects.count()),
        ("Published wellness programs", WellnessProgram.objects.filter(is_published=True).count()),
        ("Wellness check-ins in 7 days", ProgramCheckIn.objects.filter(created_at__gte=recent).count()),
        ("Open reports", CommunityReport.objects.filter(status=CommunityReport.Status.OPEN).count()),
        ("Pushes awaiting delivery", PushDelivery.objects.filter(state__in=[PushDelivery.State.PENDING,
                                                                          PushDelivery.State.RETRY]).count()),
    ]
    context = {
        **admin.site.each_context(request),
        "title": "Operations overview",
        "cards": cards,
        "window_start": recent,
    }
    return render(request, "admin/infome_analytics.html", context)
