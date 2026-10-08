from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import ADMIN_ROLES, Department, EmployeeProfile, Role, Team, User


class AccountAdminAccess:
    """Allow staff business admins to manage accounts without Django superuser status."""

    def has_module_permission(self, request):
        user = request.user
        return bool(user.is_authenticated and user.is_staff and
                    (user.is_superuser or getattr(user, "role", None) in ADMIN_ROLES))

    def has_view_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_add_permission(self, request):
        return self.has_module_permission(request)

    def has_change_permission(self, request, obj=None):
        return self.has_module_permission(request) and (obj is None or not getattr(obj, "is_superuser", False) or request.user.is_superuser)

    def has_delete_permission(self, request, obj=None):
        return self.has_change_permission(request, obj)


class ProfileInline(admin.StackedInline):
    model = EmployeeProfile
    can_delete = False


@admin.register(User)
class UserAdmin(AccountAdminAccess, BaseUserAdmin):
    ordering = ("email",)
    list_display = ("email", "full_name", "employee_id", "role", "is_active")
    list_filter = ("role", "is_active")
    search_fields = ("email", "first_name", "last_name", "employee_id")
    inlines = [ProfileInline]
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Personal", {"fields": ("first_name", "last_name", "employee_id", "profile_image")}),
        ("Access", {"fields": ("role", "is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (None, {"classes": ("wide",), "fields": ("email", "password1", "password2", "role", "employee_id")}),
    )

    def has_view_permission(self, request, obj=None):
        return super().has_view_permission(request, obj) and (obj is None or not obj.is_superuser or request.user.is_superuser)

    def get_readonly_fields(self, request, obj=None):
        fields = super().get_readonly_fields(request, obj)
        if not request.user.is_superuser:
            return (*fields, "role", "is_superuser", "is_staff", "groups", "user_permissions")
        return fields

    def get_fieldsets(self, request, obj=None):
        fieldsets = super().get_fieldsets(request, obj)
        if obj is None and not request.user.is_superuser:
            return tuple((name, {**options, "fields": tuple(field for field in options["fields"] if field != "role")})
                         for name, options in fieldsets)
        return fieldsets

    def save_model(self, request, obj, form, change):
        if not request.user.is_superuser:
            if not change:
                obj.role = Role.EMPLOYEE
                obj.is_staff = False
                obj.is_superuser = False
            elif obj.pk:
                previous = User.objects.get(pk=obj.pk)
                obj.role = previous.role
                obj.is_staff = previous.is_staff
                obj.is_superuser = previous.is_superuser
        super().save_model(request, obj, form, change)

    def get_actions(self, request):
        actions = super().get_actions(request)
        if not request.user.is_superuser:
            actions.pop("delete_selected", None)
        return actions


@admin.register(Department)
class DepartmentAdmin(AccountAdminAccess, admin.ModelAdmin):
    list_display = ("name",)


@admin.register(Team)
class TeamAdmin(AccountAdminAccess, admin.ModelAdmin):
    list_display = ("name", "department", "manager")
    search_fields = ("name",)
