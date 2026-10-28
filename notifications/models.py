from django.conf import settings
from django.db import models


class Notification(models.Model):
    class Kind(models.TextChoices):
        CHALLENGE_COMPLETE = "challenge_complete", "Challenge complete"
        CHALLENGE_REMINDER = "challenge_reminder", "Challenge reminder"
        BADGE_EARNED = "badge_earned", "Badge earned"
        COMMUNITY_REPLY = "community_reply", "Community reply"
        WATER_BREAK = "water_break", "Water break"
        MOVEMENT_BREAK = "movement_break", "Movement break"
        BMI_CHECKIN = "bmi_checkin", "Weekly BMI check-in"
        COACH_NUDGE = "coach_nudge", "Daily coach"
        WEEKLY_REVIEW = "weekly_review", "Weekly review"
        REENGAGEMENT = "reengagement", "Re-engagement"
        HR_DIGEST = "hr_digest", "HR participation digest"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    kind = models.CharField(max_length=32, choices=Kind.choices)
    title = models.CharField(max_length=120)
    body = models.CharField(max_length=240)
    event_key = models.CharField(max_length=120)
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "event_key"], name="uniq_notification_event_user")]
        indexes = [models.Index(fields=["user", "-created_at"], name="notification_user_recent_idx")]


class PushDevice(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="push_devices")
    token = models.CharField(max_length=512, unique=True)
    enabled = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)


class PushDelivery(models.Model):
    class State(models.TextChoices):
        PENDING = "pending", "Pending"
        SENT = "sent", "Sent"
        RETRY = "retry", "Retry"
        INVALID = "invalid", "Invalid"

    notification = models.ForeignKey(Notification, on_delete=models.CASCADE, related_name="deliveries")
    device = models.ForeignKey(PushDevice, on_delete=models.CASCADE, related_name="deliveries")
    state = models.CharField(max_length=12, choices=State.choices, default=State.PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["notification", "device"], name="uniq_push_notification_device")]
        indexes = [models.Index(fields=["state", "next_attempt_at"], name="push_delivery_due_idx")]
