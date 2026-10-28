from django.conf import settings
from django.db import models


class AssistantProposal(models.Model):
    """A change IVA has drafted for the employee. Nothing is saved until the employee confirms it.

    The row is also the audit trail: what was proposed, when it was answered, and what it created.
    """
    class Action(models.TextChoices):
        LOG_WATER = "log_water", "Log water"
        LOG_MEAL = "log_meal", "Log meal"
        REPEAT_MEAL = "repeat_meal", "Repeat a saved meal"
        JOIN_CHALLENGE = "join_challenge", "Join a challenge"
        SET_REMINDER = "set_reminder", "Change a reminder"
        SET_STEP_GOAL = "set_step_goal", "Change the daily step goal"
        PROGRAM_CHECKIN = "program_checkin", "Check in to a wellness program"
        START_PLAN = "start_plan", "Start a personal weekly plan"
        SAVE_PREFERENCE = "save_preference", "Remember a preference"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        CONFIRMED = "confirmed", "Confirmed"
        CANCELLED = "cancelled", "Cancelled"
        EXPIRED = "expired", "Expired"
        UNDONE = "undone", "Undone"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="assistant_proposals")
    action = models.CharField(max_length=16, choices=Action.choices)
    payload = models.JSONField(default=dict)
    summary = models.CharField(max_length=240)
    details = models.JSONField(default=list)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    result = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["user", "status"], name="assistant_user_status_idx")]
        ordering = ["-created_at"]


class ChatTurn(models.Model):
    """Saved IVA conversation, so chat survives closing the app and works across devices.

    Only the employee can read their own turns. They can clear all of it at any time.
    """
    class Role(models.TextChoices):
        USER = "user", "Employee"
        ASSISTANT = "assistant", "IVA"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="chat_turns")
    role = models.CharField(max_length=10, choices=Role.choices)
    content = models.TextField(max_length=4000)
    proposal = models.ForeignKey(AssistantProposal, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    used_context = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["user", "-created_at"], name="chat_turn_user_recent_idx")]
        ordering = ["created_at", "id"]
