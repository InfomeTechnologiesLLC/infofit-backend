from django.contrib import admin
from django.core.exceptions import PermissionDenied

from accounts.models import ADMIN_ROLES
from .models import ProgramCheckIn, ProgramEnrollment, WellnessProgram


class WellnessAdminAccess:
    def has_module_permission(self, request):
        user = request.user
        return bool(user.is_authenticated and user.is_active and user.is_staff and
                    (user.is_superuser or getattr(user, "role", None) in ADMIN_ROLES))

    def has_view_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_add_permission(self, request):
        return self.has_module_permission(request)

    def has_change_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(WellnessProgram)
class WellnessProgramAdmin(WellnessAdminAccess, admin.ModelAdmin):
    list_display = ("title", "focus_goal", "start_date", "end_date", "is_published")
    list_filter = ("is_published", "focus_goal")
    search_fields = ("title", "summary")

    def save_model(self, request, obj, form, change):
        if change and ProgramEnrollment.objects.filter(program=obj).exists():
            prior = WellnessProgram.objects.get(pk=obj.pk)
            if (prior.start_date, prior.end_date, prior.focus_goal) != (obj.start_date, obj.end_date, obj.focus_goal):
                raise PermissionDenied("Program dates and focus cannot change after enrollment.")
        obj.full_clean()
        super().save_model(request, obj, form, change)


@admin.register(ProgramEnrollment, ProgramCheckIn)
class WellnessAuditAdmin(WellnessAdminAccess, admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
