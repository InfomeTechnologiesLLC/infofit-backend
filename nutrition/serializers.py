from datetime import timedelta
from decimal import Decimal
try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # Python 3.8 hosts
    from backports.zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils import timezone
from rest_framework import serializers

from .models import Meal


class MealItemInputSerializer(serializers.Serializer):
    """One food: `food_id` ("indb:ASC144", "ifct:A003", "usda:123") or, from older app versions, `fdc_id`."""
    fdc_id = serializers.IntegerField(min_value=1, required=False)
    food_id = serializers.RegexField(r"^(indb|ifct|usda):[A-Za-z0-9_.-]{1,20}$", required=False)
    grams = serializers.DecimalField(max_digits=8, decimal_places=1, min_value=Decimal("0.1"), max_value=Decimal("5000"))

    def validate(self, data):
        if ("fdc_id" in data) == ("food_id" in data):
            raise serializers.ValidationError("Send exactly one of food_id or fdc_id for each item.")
        return data


class MealInputSerializer(serializers.Serializer):
    client_request_id = serializers.UUIDField()
    kind = serializers.ChoiceField(choices=Meal.Kind.choices)
    occurred_at = serializers.DateTimeField()
    time_zone = serializers.CharField(max_length=64)
    notes = serializers.CharField(max_length=500, required=False, allow_blank=True)
    items = MealItemInputSerializer(many=True, allow_empty=False)

    def validate_time_zone(self, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise serializers.ValidationError("Use an IANA time zone, such as Asia/Kolkata.")
        return value

    def validate_items(self, value):
        if len(value) > 10:
            raise serializers.ValidationError("A meal can contain at most 10 food items.")
        return value

    def validate_occurred_at(self, value):
        if value > timezone.now() + timedelta(days=1):
            raise serializers.ValidationError("Meal time cannot be more than one day in the future.")
        return value
