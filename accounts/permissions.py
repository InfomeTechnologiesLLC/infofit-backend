from rest_framework.permissions import BasePermission

from .models import ADMIN_ROLES, Role


class IsAdminRole(BasePermission):
    """Super Admin / Admin / HR-Admin. Enforced server-side."""

    def has_permission(self, request, view):
        u = request.user
        return bool(u and u.is_authenticated and u.role in ADMIN_ROLES)


class IsTeamManagerOrAdmin(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return bool(u and u.is_authenticated and (u.role in ADMIN_ROLES or u.role == Role.TEAM_MANAGER))
