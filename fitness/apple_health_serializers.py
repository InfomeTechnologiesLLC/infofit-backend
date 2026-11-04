"""HealthySync schema v2 (including its zero-length incremental date_range)."""
import math
from datetime import timedelta

from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import serializers

from .serializers import SourceSerializer
from .apple_health_normalization import QUANTITIES


class AwareDateTimeField(serializers.DateTimeField):
    def to_internal_value(self, value):
        try:
            parsed = parse_datetime(value) if isinstance(value, str) else value
            if parsed is None or timezone.is_naive(parsed):
                raise ValueError
        except (TypeError, ValueError, AttributeError):
            self.fail("invalid", format="ISO-8601 with a UTC offset")
        return super().to_internal_value(value)


class FiniteFloatField(serializers.FloatField):
    def to_internal_value(self, value):
        result = super().to_internal_value(value)
        if not math.isfinite(result):
            self.fail("invalid")
        return result


class AppleRecordSerializer(serializers.Serializer):
    id = serializers.CharField(max_length=128)
    start_at = AwareDateTimeField()
    end_at = AwareDateTimeField()
    source_name = serializers.CharField(max_length=255, allow_blank=True)
    source_bundle_id = serializers.CharField(max_length=255, allow_null=True, allow_blank=True, required=False)
    metadata = serializers.DictField(child=serializers.CharField(allow_blank=True, max_length=4096), default=dict)

    def validate(self, data):
        if data["end_at"] < data["start_at"]:
            raise serializers.ValidationError("Record end must not precede start.")
        if data["end_at"] > timezone.now() + timedelta(minutes=10):
            raise serializers.ValidationError("Future records are not accepted.")
        if data["end_at"] - data["start_at"] > timedelta(days=31):
            raise serializers.ValidationError("A single record may span at most 31 days.")
        return data


class AppleMetricSerializer(AppleRecordSerializer):
    type = serializers.CharField(max_length=128)
    value = FiniteFloatField()
    unit = serializers.CharField(max_length=64)

    def validate(self, data):
        data = super().validate(data)
        units = {key: value[1] for key, value in QUANTITIES.items()}
        units["sleep_analysis"] = {"seconds": 1, "s": 1, "minutes": 60, "min": 60}
        if data["type"] in units:
            if data["unit"] not in units[data["type"]] or data["value"] < 0:
                raise serializers.ValidationError("Unsupported unit or negative activity value.")
            value = data["value"] * units[data["type"]][data["unit"]]
            if not math.isfinite(value) or value > 1_000_000_000:
                raise serializers.ValidationError("Activity value is too large.")
        aggregation = data["metadata"].get("aggregation")
        if data["id"].startswith("healthkit:aggregate:") and aggregation not in ("daily_sum", "daily_average"):
            raise serializers.ValidationError("An aggregate record needs aggregation metadata.")
        if aggregation and data["type"] in units:
            if aggregation != "daily_sum" or data["type"] == "sleep_analysis":
                raise serializers.ValidationError("Expected daily_sum for activity quantities.")
            if data["start_at"] == data["end_at"]:
                raise serializers.ValidationError("An aggregate needs a nonempty interval.")
        return data


class AppleWorkoutSerializer(AppleRecordSerializer):
    activity_type = serializers.CharField(max_length=64)
    duration_seconds = FiniteFloatField(min_value=0)
    total_energy_kcal = FiniteFloatField(min_value=0, required=False, allow_null=True)
    active_energy_kcal = FiniteFloatField(min_value=0, required=False, allow_null=True)
    distance_meters = FiniteFloatField(min_value=0, required=False, allow_null=True)

    def validate(self, data):
        data = super().validate(data)
        if data["duration_seconds"] > (data["end_at"] - data["start_at"]).total_seconds() + 1:
            raise serializers.ValidationError("Workout duration exceeds its time interval.")
        return data


class AppleDeletionSerializer(serializers.Serializer):
    id = serializers.CharField(max_length=128)
    kind = serializers.ChoiceField(choices=("metric", "workout"))


class AppleDateRangeSerializer(serializers.Serializer):
    start = AwareDateTimeField()
    end = AwareDateTimeField()

    def validate(self, data):
        if data["end"] < data["start"]:
            raise serializers.ValidationError("Date range end must not precede start.")
        return data


class AppleHealthSyncSerializer(serializers.Serializer):
    schema_version = serializers.ChoiceField(choices=(2,))
    device_id = serializers.CharField(max_length=255)
    export_id = serializers.UUIDField()
    generated_at = AwareDateTimeField()
    timezone = serializers.CharField(max_length=64)
    source = serializers.ChoiceField(choices=("ios-healthkit",))
    date_range = AppleDateRangeSerializer()
    metrics = AppleMetricSerializer(many=True, default=list)
    workouts = AppleWorkoutSerializer(many=True, default=list)
    deletions = AppleDeletionSerializer(many=True, default=list)

    def validate_timezone(self, value):
        return SourceSerializer().validate_time_zone(value)

    def validate(self, data):
        if data["generated_at"] > timezone.now() + timedelta(minutes=10):
            raise serializers.ValidationError("Generation time is in the future.")
        if sum(len(data[key]) for key in ("metrics", "workouts", "deletions")) > 1000:
            raise serializers.ValidationError("At most 1000 records per batch.")
        keys = [(kind, record["id"]) for key, kind in (("metrics", "metric"), ("workouts", "workout"))
                for record in data[key]]
        keys += [(record["kind"], record["id"]) for record in data["deletions"]]
        if len(keys) != len(set(keys)):
            raise serializers.ValidationError("Duplicate or conflicting record IDs in this batch.")
        return data


class AppleHealthTokenSerializer(serializers.Serializer):
    replace_existing = serializers.BooleanField(default=False)
    new_device = serializers.BooleanField(default=False)
    device_label = serializers.CharField(max_length=100, default="iPhone / HealthySync")
