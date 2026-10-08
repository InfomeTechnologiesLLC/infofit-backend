from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class Challenge(models.Model):
    class Kind(models.TextChoices):
        STEPS = "steps", "Steps"
        RUNNING = "running", "Running time"

    slug = models.SlugField(unique=True)
    title = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    start_date = models.DateField()
    end_date = models.DateField()
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.STEPS)
    target_steps = models.PositiveIntegerField(default=0, blank=True)
    target_running_minutes = models.PositiveIntegerField(default=0, blank=True)
    reward_points = models.PositiveIntegerField()
    badge_title = models.CharField(max_length=100, blank=True)
    is_published = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def clean(self):
        if self.end_date and self.start_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": "End date must be on or after start date."})
        if self.kind == self.Kind.STEPS and not self.target_steps:
            raise ValidationError({"target_steps": "Target must be positive."})
        if self.kind == self.Kind.RUNNING and not self.target_running_minutes:
            raise ValidationError({"target_running_minutes": "Running target must be positive."})
        if not self.reward_points:
            raise ValidationError({"reward_points": "Reward must be positive."})

    def __str__(self):
        return self.title


class ChallengeEnrollment(models.Model):
    challenge = models.ForeignKey(Challenge, on_delete=models.PROTECT, related_name="enrollments")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="challenge_enrollments")
    joined_at = models.DateTimeField(auto_now_add=True)
    joined_local_date = models.DateField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["challenge", "user"], name="uniq_challenge_user")]


class ChallengeProgress(models.Model):
    enrollment = models.OneToOneField(ChallengeEnrollment, on_delete=models.CASCADE, related_name="progress")
    verified_steps = models.PositiveBigIntegerField(default=0)
    has_step_data = models.BooleanField(default=False)
    verified_running_minutes = models.PositiveIntegerField(default=0)
    is_complete = models.BooleanField(default=False)
    revision = models.PositiveIntegerField(default=0)
    evaluated_at = models.DateTimeField(null=True, blank=True)


class PointsLedger(models.Model):
    enrollment = models.ForeignKey(ChallengeEnrollment, on_delete=models.PROTECT, related_name="ledger")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="points_ledger")
    delta = models.IntegerField()
    revision = models.PositiveIntegerField()
    reason = models.CharField(max_length=20, choices=[("award", "Award"), ("correction", "Correction")])
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["enrollment", "revision"], name="uniq_challenge_ledger_revision")]
        indexes = [models.Index(fields=["user", "created_at"], name="challenge_points_user_idx")]


class BadgeAward(models.Model):
    enrollment = models.OneToOneField(ChallengeEnrollment, on_delete=models.CASCADE, related_name="badge")
    is_active = models.BooleanField(default=True)
    awarded_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
