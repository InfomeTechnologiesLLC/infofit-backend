from pathlib import Path

from django import forms
from django.http import FileResponse, Http404
from django.middleware.csrf import rotate_token
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods, require_POST
from rest_framework.response import Response
from rest_framework.views import APIView

from fitness.apple_health import token_digest
from .authentication import SESSION_KEY, TokenLoginThrottle, browser_connection, eligible_connections


class TokenLoginForm(forms.Form):
    token = forms.CharField(label="Auth token", max_length=128, strip=True,
        widget=forms.PasswordInput(attrs={"placeholder": "ahs_…", "autocomplete": "off",
            "autocapitalize": "none", "spellcheck": "false", "aria-describedby": "token-help"}))


@require_http_methods(["GET", "HEAD"])
def asset(request, name):
    """Ship this small UI with Gunicorn too, without requiring a static-file proxy."""
    types = {"app.css": "text/css", "app.js": "text/javascript", "login.js": "text/javascript"}
    if name not in types:
        raise Http404
    path = Path(__file__).resolve().parent / "static" / "employee_web" / name
    response = FileResponse(path.open("rb"), content_type=types[name] + "; charset=utf-8")
    response["Cache-Control"] = "public, max-age=0, must-revalidate"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@never_cache
@sensitive_post_parameters("token")
@require_http_methods(["GET", "POST"])
def login(request):
    if request.method == "GET" and browser_connection(request):
        return redirect("employee_web:app")
    form = TokenLoginForm(request.POST if request.method == "POST" else None)
    status = 200
    if request.method == "POST":
        throttle = TokenLoginThrottle()
        if not throttle.allow_request(request, None):
            form.add_error(None, "Too many attempts. Wait a minute, then try again.")
            status = 429
        elif form.is_valid():
            token = form.cleaned_data["token"]
            connection = eligible_connections().filter(token_hash=token_digest(token)).first() if token.startswith("ahs_") else None
            if connection is None:
                form.add_error("token", "This token is invalid or no longer active. Ask HR for your current iPhone token.")
            else:
                request.session.cycle_key()
                request.session[SESSION_KEY] = {"id": connection.pk, "digest": connection.token_hash}
                rotate_token(request)
                return redirect("employee_web:app")
    response = render(request, "employee_web/login.html", {"form": form}, status=status)
    if status == 429:
        response["Retry-After"] = str(max(1, int(throttle.wait() or 60)))
    return response


@never_cache
@require_http_methods(["GET"])
def app(request):
    connection = browser_connection(request)
    if connection is None:
        return redirect("employee_web:login")
    return render(request, "employee_web/app.html")


@never_cache
@require_POST
def logout(request):
    request.session.pop(SESSION_KEY, None)
    request.session.cycle_key()
    return redirect("employee_web:login")


class MeView(APIView):
    def get(self, request):
        user = request.user
        profile = getattr(user, "profile", None)
        return Response({"name": user.full_name, "first_name": user.first_name,
            "email": user.email, "team": profile.team.name if profile and profile.team_id else None,
            "device_label": request.auth.source.device_label,
            "last_sync": request.auth.source.last_successful_sync_at})
