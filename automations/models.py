from django.conf import settings
from django.core.validators import MaxValueValidator
from django.db import models


class AutomationSettings(models.Model):
    """Per-employee opt-ins for proactive coaching. Everything defaults to off except the HR digest (admins only)."""
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="automation_settings")
    daily_coach_enabled = models.BooleanField(default=False)
    weekly_review_enabled = models.BooleanField(default=False)
    reengagement_enabled = models.BooleanField(default=False)
    hr_digest_enabled = models.BooleanField(default=True)
    send_minute = models.PositiveSmallIntegerField(default=540, validators=[MaxValueValidator(1439)])  # 09:00 local
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
