from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class Challenge(models.Model):
    """A challenge scored only from data the phone or watch records by itself (see challenges/scoring.py)."""

    class Kind(models.TextChoices):
        STEPS = "steps", "Steps"
        RUNNING = "running", "Running time"
        DISTANCE = "distance", "Distance"
        ACTIVE_MINUTES = "active_minutes", "Workout minutes (any activity)"
        WORKOUT = "workout", "Workout minutes (one activity)"
        CALORIES = "calories", "Active calories burned"
        STEP_DAYS = "step_days", "Days reaching a step goal"
        SLEEP_NIGHTS = "sleep_nights", "Nights with enough sleep"

    class Activity(models.TextChoices):
        CYCLING = "cycling", "Cycling"
        SWIMMING = "swimming", "Swimming"
        WALKING = "walking", "Walking (recorded workouts)"
        HIKING = "hiking", "Hiking"
        YOGA = "yoga", "Yoga, pilates and stretching"
        STRENGTH = "strength", "Strength and weight training"
        HIIT = "hiit", "HIIT, boot camp and exercise classes"
        RACKET = "racket", "Racket sports (badminton, tennis, squash...)"
        TEAM_SPORTS = "team_sports", "Team sports (football, cricket, basketball...)"
        DANCING = "dancing", "Dancing"
        CARDIO_MACHINES = "cardio_machines", "Cardio machines (elliptical, rowing, stairs)"
        MARTIAL_ARTS = "martial_arts", "Martial arts and boxing"

    # Unit shown next to the target and the progress, per kind.
    UNITS = {"steps": "steps", "running": "min", "distance": "km", "active_minutes": "min", "workout": "min",
             "calories": "kcal", "step_days": "days", "sleep_nights": "nights"}

    slug = models.SlugField(unique=True)
    title = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    start_date = models.DateField()
    end_date = models.DateField()
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.STEPS)
    target_steps = models.PositiveIntegerField(default=0, blank=True)
    target_running_minutes = models.PositiveIntegerField(default=0, blank=True)
    # The target for every newer kind, in that kind's unit (km, minutes, kcal, days or nights).
    target_value = models.PositiveIntegerField(default=0, blank=True)
    # Step goal per day (step_days) or minimum sleep in minutes per night (sleep_nights).
    daily_threshold = models.PositiveIntegerField(default=0, blank=True)
    # Which workouts count for a "workout" challenge.
    activity_type = models.CharField(max_length=20, choices=Activity.choices, blank=True)
    reward_points = models.PositiveIntegerField()
    badge_title = models.CharField(max_length=100, blank=True)
    is_published = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def target(self):
        """The finishing line, in :attr:`unit`."""
        if self.kind == self.Kind.STEPS:
            return self.target_steps
        if self.kind == self.Kind.RUNNING:
            return self.target_running_minutes
        return self.target_value

    @property
    def unit(self):
        return self.UNITS.get(self.kind, "")

    @property
    def target_label(self):
        """e.g. "70,000 steps", "50 km", "12 days with 8,000+ steps", "300 min of cycling"."""
        amount = f"{self.target:,} {self.unit}"
        if self.kind == self.Kind.STEP_DAYS:
            return f"{self.target} days with {self.daily_threshold:,}+ steps"
        if self.kind == self.Kind.SLEEP_NIGHTS:
            hours = self.daily_threshold / 60
            return f"{self.target} nights with {hours:g}+ hours of sleep"
        if self.kind == self.Kind.WORKOUT:
            return f"{amount} of {self.get_activity_type_display().split(' (')[0].lower()}"
        if self.kind == self.Kind.RUNNING:
            return f"{amount} of running"
        return amount

    @property
    def counting_note(self):
        """One plain sentence on what counts, shown in the app."""
        manual = " Entries typed in by hand don't count."
        return {
            "steps": "Steps your phone or watch records count from the day after you join." + manual,
            "running": "Only running workouts recorded after you join count; walking steps don't." + manual,
            "distance": "Distance your phone or watch records counts from the day after you join." + manual,
            "calories": "Active calories your watch or fitness app records count from the day after you join." + manual,
            "active_minutes": "Recorded workouts of 10 minutes or more count, from the moment you join." + manual,
            "workout": "Recorded workouts of this activity, 10 minutes or more, count from the moment you join." + manual,
            "step_days": "Each day after you join with enough recorded steps counts once." + manual,
            "sleep_nights": "Each night your watch or phone records enough sleep counts once." + manual,
        }.get(self.kind, "")

    def progress_label(self, progress):
        """e.g. "12.4 of 50 km", or None when nothing has synced yet."""
        if not progress or not progress.has_step_data:
            return None
        value = progress.progress_value
        shown = f"{value:,.1f}" if self.kind == self.Kind.DISTANCE else f"{int(value):,}"
        return f"{shown} of {self.target:,} {self.unit}"

    def clean(self):
        if self.end_date and self.start_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": "End date must be on or after start date."})
        if self.kind == self.Kind.STEPS and not self.target_steps:
            raise ValidationError({"target_steps": "Target must be positive."})
        if self.kind == self.Kind.RUNNING and not self.target_running_minutes:
            raise ValidationError({"target_running_minutes": "Running target must be positive."})
        if self.kind not in (self.Kind.STEPS, self.Kind.RUNNING) and not self.target_value:
            raise ValidationError({"target_value": "Enter a target."})
        if self.kind == self.Kind.WORKOUT and not self.activity_type:
            raise ValidationError({"activity_type": "Choose which activity counts."})
        if self.kind == self.Kind.STEP_DAYS and not 1000 <= self.daily_threshold <= 50000:
            raise ValidationError({"daily_threshold": "Enter a daily step goal between 1,000 and 50,000."})
        if self.kind == self.Kind.SLEEP_NIGHTS and not 240 <= self.daily_threshold <= 720:
            raise ValidationError({"daily_threshold": "Enter minimum sleep between 240 and 720 minutes (4 to 12 hours)."})
        if self.kind in (self.Kind.STEP_DAYS, self.Kind.SLEEP_NIGHTS) and self.start_date and self.end_date:
            # The joining day never counts, so the most anyone can reach is the number of days minus one.
            possible = (self.end_date - self.start_date).days
            if self.target_value > possible:
                raise ValidationError({"target_value": f"At most {possible} for these dates (the joining day does not count)."})
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
    # Progress in the challenge's own unit, for every kind (steps, minutes, km, kcal, days or nights).
    progress_value = models.FloatField(default=0)
    # Days whose totals were cut down to the daily limit because they looked impossible (shown to staff).
    flagged_days = models.PositiveSmallIntegerField(default=0)
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
