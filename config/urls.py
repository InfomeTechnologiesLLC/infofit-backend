from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path
from django.views.generic import RedirectView

from accounts.admin_analytics import analytics
from fitness.apple_health_views import AppleHealthSyncView


def health(_request):
    return JsonResponse({"status": "ok"})


admin.site.index_template = "admin/infome_index.html"

urlpatterns = [
    path("", RedirectView.as_view(pattern_name="portal:dashboard", permanent=False)),
    path("portal/", include("portal.urls")),
    path("app/", include("employee_web.urls")),
    path("admin/analytics/", admin.site.admin_view(analytics), name="admin_analytics"),
    path("admin/", admin.site.urls),
    path("healthz/", health),
    # HealthySync appends this exact path (without a trailing slash) to its configured base URL.
    path("api/apple-health/sync", AppleHealthSyncView.as_view(), name="apple_health_sync"),
    path("api/apple-health/sync/", AppleHealthSyncView.as_view()),
    path("api/v1/", include("accounts.urls")),
    path("api/v1/fitness/", include("fitness.urls")),
    path("api/v1/challenges/", include("challenges.urls")),
    path("api/v1/community/", include("community.urls")),
    path("api/v1/ai/", include("ai_service.urls")),
    path("api/v1/notifications/", include("notifications.urls")),
    path("api/v1/nutrition/", include("nutrition.urls")),
    path("api/v1/wellness/", include("wellness.urls")),
    path("api/v1/automations/", include("automations.urls")),
    path("api/v1/assistant/", include("assistant.urls")),
]
