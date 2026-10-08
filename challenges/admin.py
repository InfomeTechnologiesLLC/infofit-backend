from django.contrib import admin
from django.core.exceptions import PermissionDenied

from accounts.models import ADMIN_ROLES
from .models import BadgeAward, Challenge, ChallengeEnrollment, ChallengeProgress, PointsLedger


class AdminRoleOnly:
    def has_module_permission(self, request):
        user = request.user
        return bool(user.is_authenticated and (user.is_superuser or getattr(user, "role", None) in ADMIN_ROLES))

    def has_view_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_add_permission(self, request):
        return self.has_module_permission(request)

    def has_change_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Challenge)
class ChallengeAdmin(AdminRoleOnly, admin.ModelAdmin):
    list_display = ("title", "start_date", "end_date", "target_steps", "reward_points", "is_published")
    list_filter = ("is_published",)
    search_fields = ("title", "slug")

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.is_published:
            return ("slug", "start_date", "end_date", "target_steps", "reward_points", "badge_title")
        return ()

    def save_model(self, request, obj, form, change):
        if change and ChallengeEnrollment.objects.filter(challenge=obj).exists():
            old = Challenge.objects.get(pk=obj.pk)
            fixed = ("slug", "start_date", "end_date", "target_steps", "reward_points", "badge_title")
            if any(getattr(old, field) != getattr(obj, field) for field in fixed):
                raise PermissionDenied("Challenge scoring rules cannot change after enrollment.")
        obj.full_clean()
        super().save_model(request, obj, form, change)


@admin.register(ChallengeEnrollment, ChallengeProgress, PointsLedger, BadgeAward)
class ChallengeAuditAdmin(AdminRoleOnly, admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
