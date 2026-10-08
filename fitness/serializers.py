from datetime import timedelta
import math
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils import timezone
from rest_framework import serializers

METRICS = ("steps", "distance_m", "active_kcal", "exercise_minutes", "sleep_minutes")
STATUSES = ("available", "no_data", "permission_unavailable", "unsupported")


class SourceSerializer(serializers.Serializer):
    installation_id = serializers.UUIDField()
    device_label = serializers.CharField(max_length=100, required=False, allow_blank=True)
    time_zone = serializers.CharField(max_length=64)
    replace_existing = serializers.BooleanField(default=False)

    def validate_time_zone(self, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise serializers.ValidationError("Use an IANA time zone.")
        return value


class RecordSerializer(serializers.Serializer):
    record_id = serializers.CharField(max_length=128)
    origin_package = serializers.CharField(max_length=255)
    start_at = serializers.DateTimeField()
    end_at = serializers.DateTimeField()
    exercise_type = serializers.CharField(max_length=64, required=False)

    def validate(self, data):
        if data["start_at"] >= data["end_at"]:
            raise serializers.ValidationError("Record end must follow start.")
        return data


class DaySerializer(serializers.Serializer):
    local_date = serializers.DateField()
    observed_at = serializers.DateTimeField()
    steps = serializers.IntegerField(min_value=0, max_value=1000000, allow_null=True, required=False, default=None)
    distance_m = serializers.FloatField(min_value=0, max_value=1000000, allow_null=True, required=False, default=None)
    active_kcal = serializers.FloatField(min_value=0, max_value=100000, allow_null=True, required=False, default=None)
    exercise_minutes = serializers.IntegerField(min_value=0, max_value=1440, allow_null=True, required=False, default=None)
    sleep_minutes = serializers.IntegerField(min_value=0, max_value=1440, allow_null=True, required=False, default=None)
    metric_status = serializers.DictField(child=serializers.ChoiceField(choices=STATUSES))
    exercises = RecordSerializer(many=True)
    sleep = RecordSerializer(many=True)

    def validate_distance_m(self, value):
        if value is not None and not math.isfinite(value):
            raise serializers.ValidationError("Use a finite distance.")
        return value

    def validate_active_kcal(self, value):
        if value is not None and not math.isfinite(value):
            raise serializers.ValidationError("Use finite calories.")
        return value

    def validate(self, data):
        if set(data["metric_status"]) != set(METRICS):
            raise serializers.ValidationError("A status is required for each metric.")
        for metric in METRICS:
            has_value = data[metric] is not None
            if has_value != (data["metric_status"][metric] == "available"):
                raise serializers.ValidationError({metric: "Value and status disagree."})
        if data["observed_at"] > timezone.now() + timedelta(minutes=10):
            raise serializers.ValidationError("Observation time is in the future.")
        for kind in ("exercises", "sleep"):
            ids = [record["record_id"] for record in data[kind]]
            if len(ids) != len(set(ids)):
                raise serializers.ValidationError({kind: "Duplicate record IDs."})
        return data


class SyncSerializer(serializers.Serializer):
    installation_id = serializers.UUIDField()
    idempotency_key = serializers.UUIDField()
    sequence = serializers.IntegerField(min_value=1)
    time_zone = serializers.CharField(max_length=64)
    days = DaySerializer(many=True, allow_empty=False)

    def validate_time_zone(self, value):
        return SourceSerializer().validate_time_zone(value)

    def validate(self, data):
        if len(data["days"]) > 31:
            raise serializers.ValidationError("At most 31 days per batch.")
        dates = [day["local_date"] for day in data["days"]]
        if len(dates) != len(set(dates)):
            raise serializers.ValidationError("Duplicate dates in batch.")
        zone = ZoneInfo(data["time_zone"])
        today = timezone.now().astimezone(zone).date()
        for day in data["days"]:
            if day["local_date"] > today:
                raise serializers.ValidationError("Future activity dates are not accepted.")
            for record in day["exercises"] + day["sleep"]:
                if record["end_at"].astimezone(zone).date() != day["local_date"]:
                    raise serializers.ValidationError("Record must belong to its local end date.")
        for kind in ("exercises", "sleep"):
            record_ids = [record["record_id"] for day in data["days"] for record in day[kind]]
            if len(record_ids) != len(set(record_ids)):
                raise serializers.ValidationError({kind: "Duplicate record IDs across days."})
        return data
