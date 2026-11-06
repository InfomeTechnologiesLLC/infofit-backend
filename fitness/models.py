from django.conf import settings
from django.db import models

from .migration_compat import database_default


class FitnessSource(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="fitness_sources")
    installation_id = models.UUIDField()
    device_label = models.CharField(max_length=100, blank=True)
    kind = models.CharField(max_length=20, default="health_connect", **database_default("health_connect"),
                            choices=[("health_connect", "Health Connect"), ("apple_health", "Apple Health")])
    last_sequence = models.PositiveBigIntegerField(default=0)
    last_successful_sync_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "installation_id"], name="uniq_fitness_installation")]


class FitnessAccount(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="fitness_account")
    active_source = models.ForeignKey(FitnessSource, null=True, blank=True, on_delete=models.SET_NULL)
    time_zone = models.CharField(max_length=64, default="UTC")


class DailyActivity(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    source = models.ForeignKey(FitnessSource, on_delete=models.CASCADE)
    local_date = models.DateField()
    time_zone = models.CharField(max_length=64)
    steps = models.PositiveIntegerField(null=True, blank=True)
    distance_m = models.FloatField(null=True, blank=True)
    active_kcal = models.FloatField(null=True, blank=True)
    exercise_minutes = models.PositiveIntegerField(null=True, blank=True)
    sleep_minutes = models.PositiveIntegerField(null=True, blank=True)
    # Parts of the day's totals that someone typed in by hand in a fitness app (Health Connect "manual entry").
    # Challenges subtract these, so only data the phone or watch recorded by itself counts. Null = app too old to say.
    manual_steps = models.PositiveIntegerField(null=True, blank=True)
    manual_distance_m = models.FloatField(null=True, blank=True)
    manual_active_kcal = models.FloatField(null=True, blank=True)
    metric_status = models.JSONField(default=dict)
    # Apple Health summary-first daily credit, excluding explicitly manual readings.
    # Null means unavailable; absent keys retain Android's total-minus-manual behavior.
    verified_metrics = models.JSONField(default=dict, **database_default({}))
    observed_at = models.DateTimeField()
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "local_date"], name="uniq_fitness_user_day")]
        indexes = [models.Index(fields=["user", "local_date"], name="fitness_user_day_idx")]

    def verified_value(self, metric):
        if metric in self.verified_metrics:
            return self.verified_metrics[metric]
        value = getattr(self, metric)
        if value is None:
            return None
        return max(0.0, float(value) - float(getattr(self, "manual_" + metric, None) or 0))


class FitnessRecord(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    source = models.ForeignKey(FitnessSource, on_delete=models.CASCADE)
    record_id = models.CharField(max_length=128)
    origin_package = models.CharField(max_length=255)
    local_date = models.DateField()
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    # How Health Connect says the record was made. "manual" (typed in by hand) never counts for challenges.
    recording_method = models.CharField(max_length=10, default="unknown")

    class Meta:
        abstract = True


class ExerciseRecord(FitnessRecord):
    exercise_type = models.CharField(max_length=64)
    # HealthKit duration excludes pauses; null retains Health Connect's interval semantics.
    duration_seconds = models.FloatField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "source", "record_id"], name="uniq_fitness_exercise_record")]
        indexes = [models.Index(fields=["user", "local_date"], name="fitness_exercise_day_idx")]


class SleepRecord(FitnessRecord):
    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "source", "record_id"], name="uniq_fitness_sleep_record")]
        indexes = [models.Index(fields=["user", "local_date"], name="fitness_sleep_day_idx")]


class FitnessSyncBatch(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    source = models.ForeignKey(FitnessSource, on_delete=models.CASCADE)
    idempotency_key = models.UUIDField()
    payload_hash = models.CharField(max_length=64)
    sequence = models.PositiveBigIntegerField()
    response = models.JSONField(default=dict)
    processed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "idempotency_key"], name="uniq_fitness_sync_key")]


class AppleHealthConnection(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="apple_health")
    source = models.OneToOneField(FitnessSource, on_delete=models.CASCADE)
    token_hash = models.CharField(max_length=64, blank=True, db_index=True)
    device_id = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    token_updated_at = models.DateTimeField(auto_now=True)


class AppleHealthRecord(models.Model):
    source = models.ForeignKey(FitnessSource, on_delete=models.CASCADE)
    record_id = models.CharField(max_length=128)
    kind = models.CharField(max_length=10, choices=[("metric", "Metric"), ("workout", "Workout")])
    record_type = models.CharField(max_length=128, blank=True)
    payload = models.JSONField(default=dict)
    time_zone = models.CharField(max_length=64)
    start_at = models.DateTimeField(null=True)
    end_at = models.DateTimeField(null=True)
    first_date = models.DateField(null=True)
    last_date = models.DateField(null=True)
    generated_at = models.DateTimeField()
    # Tombstones also retain unknown IDs, so a delayed upload cannot resurrect a deleted sample.
    deleted = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["source", "kind", "record_id"], name="uniq_apple_health_record")]
        indexes = [models.Index(fields=["source", "first_date", "last_date"], name="apple_health_dates_idx")]


class AppleHealthSyncBatch(models.Model):
    source = models.ForeignKey(FitnessSource, on_delete=models.CASCADE)
    export_id = models.UUIDField()
    payload_hash = models.CharField(max_length=64)
    response = models.JSONField(default=dict)
    processed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["source", "export_id"], name="uniq_apple_health_export")]
