from django.contrib import admin

from .models import Notification, PushDelivery, PushDevice


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "kind", "title", "created_at", "read_at")
    list_filter = ("kind", "created_at")
    search_fields = ("user__email", "title")
    readonly_fields = ("user", "kind", "title", "body", "event_key", "created_at", "read_at")
    def has_add_permission(self, request):
        return False


@admin.register(PushDelivery)
class PushDeliveryAdmin(admin.ModelAdmin):
    list_display = ("id", "notification", "state", "attempts", "next_attempt_at", "sent_at")
    list_filter = ("state",)
    readonly_fields = ("notification", "device", "state", "attempts", "next_attempt_at", "sent_at")
    def has_add_permission(self, request):
        return False


@admin.register(PushDevice)
class PushDeviceAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "enabled", "updated_at")
    exclude = ("token",)
    def has_add_permission(self, request):
        return False
