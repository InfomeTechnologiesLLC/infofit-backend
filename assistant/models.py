from django.conf import settings
from django.db import models


class AssistantProposal(models.Model):
    """A change IVA has drafted for the employee. Nothing is saved until the employee confirms it.

    The row is also the audit trail: what was proposed, when it was answered, and what it created.
    """
    class Action(models.TextChoices):
        LOG_WATER = "log_water", "Log water"
        LOG_MEAL = "log_meal", "Log meal"

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
