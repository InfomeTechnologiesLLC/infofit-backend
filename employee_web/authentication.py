"""A revocable employee browser session, separate from Django's staff login."""
from django.db.models import F
from rest_framework.authentication import BaseAuthentication, SessionAuthentication
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.throttling import SimpleRateThrottle

from accounts.models import Role
from fitness.models import AppleHealthConnection


SESSION_KEY = "employee_web_connection"


def eligible_connections():
    return AppleHealthConnection.objects.select_related("user", "source").filter(
        user__is_active=True, user__is_staff=False, user__is_superuser=False,
        user__role__in=(Role.EMPLOYEE, Role.TEAM_MANAGER),
        source_id=F("user__fitness_account__active_source_id"),
    ).exclude(token_hash="")


def browser_connection(request):
    identity = request.session.get(SESSION_KEY)
    if not isinstance(identity, dict) or not identity.get("digest"):
        return None
    connection = eligible_connections().filter(pk=identity.get("id"), token_hash=identity["digest"]).first()
    if connection is None:
        request.session.pop(SESSION_KEY, None)
    return connection


class EmployeeSessionAuthentication(BaseAuthentication):
    def authenticate(self, request):
        connection = browser_connection(request)
        if connection is None:
            raise AuthenticationFailed("Please sign in with your current HealthySync token.")
        SessionAuthentication().enforce_csrf(request)
        return connection.user, connection

    def authenticate_header(self, request):
        return "Session"


class TokenLoginThrottle(SimpleRateThrottle):
    scope = "login"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": "employee_web_login", "ident": self.get_ident(request)}
