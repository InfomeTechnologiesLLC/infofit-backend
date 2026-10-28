from django.utils import timezone
from django.db import transaction
from rest_framework import serializers, status
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsAdminRole

from . import services
from .models import AutomationSettings, StepGoalSuggestion


class AutomationSettingsSerializer(serializers.ModelSerializer):
    send_minute = serializers.IntegerField(min_value=0, max_value=1439, required=False)
    goal_nudge_minute = serializers.IntegerField(min_value=0, max_value=1439, required=False)

    class Meta:
        model = AutomationSettings
        fields = ["daily_coach_enabled", "weekly_review_enabled", "reengagement_enabled", "hr_digest_enabled", "send_minute",
                  "goal_nudge_enabled", "goal_nudge_minute", "challenge_alerts_enabled", "meal_prompts_enabled"]


class AutomationSettingsView(APIView):
    def get(self, request):
        prefs, _ = AutomationSettings.objects.get_or_create(user=request.user)
        return Response(AutomationSettingsSerializer(prefs).data)

    def patch(self, request):
        prefs, _ = AutomationSettings.objects.get_or_create(user=request.user)
        serializer = AutomationSettingsSerializer(prefs, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


def _suggestion(s):
    return {"id": s.pk, "current_goal": s.current_goal, "suggested_goal": s.suggested_goal, "reason": s.reason}


class StepGoalSuggestionView(APIView):
    def get(self, request):
        pending = StepGoalSuggestion.objects.filter(user=request.user, status=StepGoalSuggestion.Status.PENDING).first()
        return Response({"suggestion": _suggestion(pending) if pending else None})


class StepGoalActionView(APIView):
    """accept applies the suggested goal to the profile; dismiss just closes it."""
    def post(self, request, pk, action):
        if action not in ("accept", "dismiss"):
            raise NotFound()
        with transaction.atomic():
            suggestion = StepGoalSuggestion.objects.select_for_update().filter(
                user=request.user, pk=pk, status=StepGoalSuggestion.Status.PENDING).first()
            if not suggestion:
                raise NotFound("Suggestion not found or already answered.")
            suggestion.status = (StepGoalSuggestion.Status.ACCEPTED if action == "accept"
                                 else StepGoalSuggestion.Status.DISMISSED)
            suggestion.resolved_at = timezone.now()
            suggestion.save(update_fields=["status", "resolved_at"])
            goal = request.user.profile.daily_step_goal
            if action == "accept":
                request.user.profile.daily_step_goal = suggestion.suggested_goal
                request.user.profile.save(update_fields=["daily_step_goal", "updated_at"])
                goal = suggestion.suggested_goal
        return Response({"status": suggestion.status, "daily_step_goal": goal}, status=status.HTTP_200_OK)


class ChallengeSuggestionsView(APIView):
    def get(self, request):
        today = timezone.now().astimezone(services.user_zone(request.user)).date()
        return Response({"suggestions": services.suggest_challenges(request.user, today)})


class ReminderTimingView(APIView):
    def get(self, request):
        return Response({"water": services.suggest_water_reminder_start(request.user)})


class HrDigestView(APIView):
    permission_classes = [IsAdminRole]

    def get(self, request):
        return Response(services.hr_digest())
