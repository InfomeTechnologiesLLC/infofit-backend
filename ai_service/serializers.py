from rest_framework import serializers


class HistoryTurnSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=("user", "assistant"))
    content = serializers.CharField(max_length=1000, trim_whitespace=True)


class ChatSerializer(serializers.Serializer):
    message = serializers.CharField(max_length=1000, trim_whitespace=True)
    history = HistoryTurnSerializer(many=True, required=False)

    def validate_history(self, value):
        if len(value) > 8:
            raise serializers.ValidationError("Send at most eight previous messages.")
        return value


class LensSerializer(serializers.Serializer):
    image = serializers.ImageField()

    def validate_image(self, image):
        if image.size > 3 * 1024 * 1024:
            raise serializers.ValidationError("Photo must be smaller than 3 MB.")
        if image.content_type not in ("image/jpeg", "image/png"):
            raise serializers.ValidationError("Use a JPEG or PNG photo.")
        if image.image.format not in ("JPEG", "PNG"):
            raise serializers.ValidationError("Use a valid JPEG or PNG photo.")
        return image


class FoodSearchSerializer(serializers.Serializer):
    q = serializers.CharField(min_length=2, max_length=80, trim_whitespace=True)


class CoachingSerializer(serializers.Serializer):
    period = serializers.ChoiceField(choices=("daily", "week", "challenge"))
    challenge_id = serializers.IntegerField(min_value=1, required=False)

    def validate(self, data):
        if data["period"] == "challenge" and "challenge_id" not in data:
            raise serializers.ValidationError({"challenge_id": "Choose a joined challenge."})
        return data


class ConfirmedFoodSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100, trim_whitespace=True)
    grams = serializers.FloatField(min_value=1, max_value=2000)


class MealGuidanceSerializer(serializers.Serializer):
    foods = ConfirmedFoodSerializer(many=True)

    def validate_foods(self, value):
        if not 1 <= len(value) <= 10:
            raise serializers.ValidationError("Include one to ten confirmed foods.")
        return value
