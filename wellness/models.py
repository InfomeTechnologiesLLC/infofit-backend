from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from accounts.models import EmployeeProfile


class WellnessProgram(models.Model):
    title = models.CharField(max_length=120)
    summary = models.CharField(max_length=280)
    daily_action = models.TextField(max_length=1000)
    focus_goal = models.CharField(
        max_length=20,
        choices=[("all", "All goals"), *EmployeeProfile.FitnessGoal.choices],
        default="all",
    )
    start_date = models.DateField()
    end_date = models.DateField()
    is_published = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def clean(self):
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": "End date must be on or after start date."})

    def __str__(self):
        return self.title


class ProgramEnrollment(models.Model):
    program = models.ForeignKey(WellnessProgram, on_delete=models.PROTECT, related_name="enrollments")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="wellness_enrollments")
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["program", "user"], name="uniq_wellness_enrollment")]


class ProgramCheckIn(models.Model):
    enrollment = models.ForeignKey(ProgramEnrollment, on_delete=models.CASCADE, related_name="check_ins")
    local_date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["enrollment", "local_date"], name="uniq_wellness_checkin_day")]


class HydrationLog(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="hydration_logs")
    client_request_id = models.UUIDField()
    volume_ml = models.PositiveSmallIntegerField()
    local_date = models.DateField()
    time_zone = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "client_request_id"], name="uniq_hydration_request")]
        indexes = [models.Index(fields=["user", "local_date"], name="hydration_user_day_idx")]


class ReminderSettings(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="reminder_settings")
    water_enabled = models.BooleanField(default=False)
    movement_enabled = models.BooleanField(default=False)
    water_interval_minutes = models.PositiveSmallIntegerField(default=90)
    movement_interval_minutes = models.PositiveSmallIntegerField(default=60)
    # Legacy whole-hour window, kept in step with the minute fields for older app versions.
    start_hour = models.PositiveSmallIntegerField(default=9)
    end_hour = models.PositiveSmallIntegerField(default=19)
    # Minutes after local midnight (9:30 = 570). end_minute may be 1440 (midnight, end of day).
    start_minute = models.PositiveSmallIntegerField(default=540)
    end_minute = models.PositiveSmallIntegerField(default=1140)
    challenge_enabled = models.BooleanField(default=False)
    bmi_enabled = models.BooleanField(default=False)
    challenge_minute = models.PositiveSmallIntegerField(default=1020)
    time_zone = models.CharField(max_length=64, default="UTC")
    updated_at = models.DateTimeField(auto_now=True)


def bmi_category(bmi):
    """Standard adult BMI bands (WHO). A screening figure, not a diagnosis."""
    if bmi < 18.5:
        return "underweight"
    if bmi < 25:
        return "healthy_range"
    if bmi < 30:
        return "overweight"
    return "obesity"


class BodyMeasurement(models.Model):
    """Self-reported height/weight. One row per employee per local day; BMI is computed on the server."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="body_measurements")
    height_cm = models.DecimalField(max_digits=4, decimal_places=1)
    weight_kg = models.DecimalField(max_digits=5, decimal_places=1)
    bmi = models.DecimalField(max_digits=4, decimal_places=1)
    local_date = models.DateField()
    time_zone = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "local_date"], name="uniq_body_measurement_day")]
        indexes = [models.Index(fields=["user", "-local_date"], name="body_measure_user_day_idx")]
        ordering = ["-local_date"]
