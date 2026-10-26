from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from .models import EmployeeProfile, User


class LoginSerializer(TokenObtainPairSerializer):
    """Standard JWT pair plus a small user summary for the app."""

    def validate(self, attrs):
        attrs["email"] = attrs.get("email", "").strip().lower()
        data = super().validate(attrs)
        data["user"] = {
            "id": self.user.id,
            "email": self.user.email,
            "full_name": self.user.full_name,
            "role": self.user.role,
        }
        return data


class ProfileSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(source="user.id", read_only=True)
    email = serializers.EmailField(source="user.email", read_only=True)
    employee_id = serializers.CharField(source="user.employee_id", read_only=True)
    role = serializers.CharField(source="user.role", read_only=True)
    first_name = serializers.CharField(source="user.first_name", required=False, allow_blank=True, max_length=100)
    last_name = serializers.CharField(source="user.last_name", required=False, allow_blank=True, max_length=100)
    department = serializers.CharField(source="department.name", read_only=True, default=None)
    team = serializers.CharField(source="team.name", read_only=True, default=None)
    onboarding_completed = serializers.SerializerMethodField()

    def get_onboarding_completed(self, obj):
        return obj.onboarding_completed_at is not None

    class Meta:
        model = EmployeeProfile
        fields = [
            "id", "email", "employee_id", "role", "first_name", "last_name",
            "department", "team", "designation", "branch", "date_of_joining",
            "fitness_goal", "daily_step_goal", "wellness_preferences", "onboarding_completed",
        ]
        # Organisational fields can only be changed by admins, never by the employee.
        read_only_fields = ["designation", "branch", "date_of_joining"]

    def validate_daily_step_goal(self, value):
        if not 1000 <= value <= 100000:
            raise serializers.ValidationError("Step goal must be between 1,000 and 100,000.")
        return value

    def validate_wellness_preferences(self, value):
        if not isinstance(value, dict) or len(str(value)) > 4000:
            raise serializers.ValidationError("Must be a small JSON object.")
        return value

    def update(self, instance, validated_data):
        user_data = validated_data.pop("user", {})
        for k, v in user_data.items():
            setattr(instance.user, k, v)
        if user_data:
            instance.user.save(update_fields=list(user_data))
        return super().update(instance, validated_data)


class PasswordChangeSerializer(serializers.Serializer):
    old_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True)

    def validate_old_password(self, value):
        if not self.context["request"].user.check_password(value):
            raise serializers.ValidationError("Current password is incorrect.")
        return value

    def validate_new_password(self, value):
        validate_password(value, self.context["request"].user)
        return value


class LogoutSerializer(serializers.Serializer):
    refresh = serializers.CharField()
