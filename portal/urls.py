from django.contrib.auth.views import LogoutView
from django.urls import path

from . import views

app_name = "portal"

urlpatterns = [
    path("login/", views.PortalLoginView.as_view(), name="login"),
    path("logout/", LogoutView.as_view(next_page="portal:login"), name="logout"),
    path("", views.dashboard, name="dashboard"),
    path("insights/", views.insights, name="insights"),
    path("notifications/", views.notifications_health, name="notifications"),
    path("ai/draft/", views.ai_draft, name="ai_draft"),
    path("points/", views.points_leaderboard, name="points"),
    path("employees/", views.employees, name="employees"),
    path("employees/new/", views.employee_form, name="employee_new"),
    path("employees/<int:pk>/", views.employee_detail, name="employee_detail"),
    path("employees/<int:pk>/edit/", views.employee_form, name="employee_edit"),
    path("employees/<int:pk>/delete/", views.employee_delete, name="employee_delete"),
    path("employees/<int:pk>/password/", views.employee_password, name="employee_password"),
    path("employees/<int:pk>/active/", views.employee_toggle_active, name="employee_toggle_active"),
    path("employees/<int:pk>/apple-health/", views.employee_apple_health, name="employee_apple_health"),
    path("departments/", views.records, {"section": "departments"}, name="departments"),
    path("departments/new/", views.record_form, {"section": "departments"}, name="department_new"),
    path("departments/<int:pk>/edit/", views.record_form, {"section": "departments"}, name="department_edit"),
    path("teams/", views.records, {"section": "teams"}, name="teams"),
    path("teams/new/", views.record_form, {"section": "teams"}, name="team_new"),
    path("teams/<int:pk>/edit/", views.record_form, {"section": "teams"}, name="team_edit"),
    path("challenges/", views.records, {"section": "challenges"}, name="challenges"),
    path("challenges/new/", views.record_form, {"section": "challenges"}, name="challenge_new"),
    path("challenges/<int:pk>/", views.challenge_detail, name="challenge_detail"),
    path("challenges/<int:pk>/edit/", views.record_form, {"section": "challenges"}, name="challenge_edit"),
    path("programs/", views.records, {"section": "programs"}, name="programs"),
    path("programs/new/", views.record_form, {"section": "programs"}, name="program_new"),
    path("programs/<int:pk>/edit/", views.record_form, {"section": "programs"}, name="program_edit"),
    path("posts/", views.posts, name="posts"),
    path("posts/<int:pk>/", views.post_detail, name="post_detail"),
    path("posts/<int:pk>/photo/", views.post_photo, name="post_photo"),
    path("posts/<int:pk>/hide/", views.hide_post, name="hide_post"),
    path("posts/<int:pk>/approve/", views.approve_post, name="approve_post"),
    path("reports/", views.reports, name="reports"),
    path("reports/<int:pk>/", views.report_detail, name="report_detail"),
    path("reports/<int:pk>/resolve/", views.resolve_report, name="resolve_report"),
    path("reports/<int:pk>/hide-post/", views.hide_reported_post, name="hide_reported_post"),
]
