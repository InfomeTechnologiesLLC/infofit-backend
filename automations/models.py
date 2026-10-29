from django.conf import settings
from django.core.validators import MaxValueValidator
from django.db import models


class AutomationSettings(models.Model):
    """Per-employee choices for proactive coaching. Everything is on by default; each person can turn items off in Settings."""
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="automation_settings")
    daily_coach_enabled = models.BooleanField(default=True)
    weekly_review_enabled = models.BooleanField(default=True)
    reengagement_enabled = models.BooleanField(default=True)
    hr_digest_enabled = models.BooleanField(default=True)
    send_minute = models.PositiveSmallIntegerField(default=540, validators=[MaxValueValidator(1439)])  # 09:00 local
    # "You're 1,200 steps from your goal" in the evening, only when today's steps synced recently.
    goal_nudge_enabled = models.BooleanField(default=True)
    goal_nudge_minute = models.PositiveSmallIntegerField(default=1140, validators=[MaxValueValidator(1439)])  # 19:00
    # "Challenge ends tomorrow" at send_minute, and news of newly published challenges and wellness programs.
    challenge_alerts_enabled = models.BooleanField(default=True)
    # "Had lunch? Log it" shortly after the employee's own usual meal times, learned from saved meals.
    meal_prompts_enabled = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)


class AutomationLog(models.Model):
    """One row per automated message per local day: makes sends idempotent and enforces the daily cap."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="automation_logs")
    kind = models.CharField(max_length=32)
    local_date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "kind", "local_date"], name="uniq_automation_log_day")]
        indexes = [models.Index(fields=["user", "local_date"], name="automation_user_day_idx")]


class StepGoalSuggestion(models.Model):
    """A proposed daily step goal. Never applied without the employee accepting it."""
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        DISMISSED = "dismissed", "Dismissed"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="step_goal_suggestions")
    current_goal = models.PositiveIntegerField()
    suggested_goal = models.PositiveIntegerField()
    reason = models.CharField(max_length=200)
    week_start = models.DateField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "week_start"], name="uniq_goal_suggestion_week")]
        ordering = ["-created_at"]
