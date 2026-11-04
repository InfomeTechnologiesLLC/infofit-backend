"""Explicit employee-only surface, using the same handlers as the Android app."""
from django.urls import path
from django.views.decorators.cache import never_cache
from rest_framework.permissions import IsAuthenticated
from rest_framework.renderers import JSONRenderer

from challenges import views as challenges
from community import views as community
from fitness import views as fitness
from . import views
from .authentication import EmployeeSessionAuthentication


app_name = "employee_web"


def employee_api(view, methods):
    return never_cache(view.as_view(authentication_classes=[EmployeeSessionAuthentication],
        permission_classes=[IsAuthenticated], renderer_classes=[JSONRenderer],
        http_method_names=[*methods, "head", "options"]))


urlpatterns = [
    path("assets/<str:name>", views.asset, name="asset"),
    path("", views.app, name="app"),
    path("login/", views.login, name="login"),
    path("logout/", views.logout, name="logout"),
    path("api/me/", employee_api(views.MeView, ["get"])),
    path("api/home/", employee_api(fitness.HomeView, ["get"])),
    path("api/steps/", employee_api(fitness.StepSummaryView, ["get"])),
    path("api/activity/", employee_api(fitness.ActivityView, ["get"])),
    path("api/rankings/", employee_api(challenges.RankingsView, ["get", "post"])),
    path("api/challenges/", employee_api(challenges.ChallengeListView, ["get"])),
    path("api/challenges/mine/", employee_api(challenges.MyChallengesView, ["get"])),
    path("api/challenges/<int:pk>/join/", employee_api(challenges.JoinView, ["post"])),
    path("api/community/posts/", employee_api(community.PostListView, ["get", "post"])),
    path("api/community/posts/photos/", employee_api(community.PhotoPostView, ["post"])),
    path("api/community/posts/<int:pk>/", employee_api(community.PostDetailView, ["get", "delete"])),
    path("api/community/posts/<int:pk>/photo/", employee_api(community.PostPhotoView, ["get"])),
    path("api/community/posts/<int:pk>/comments/", employee_api(community.CommentListView, ["get", "post"])),
    path("api/community/posts/<int:pk>/reactions/<str:kind>/", employee_api(community.ReactionView, ["put"])),
    path("api/community/posts/<int:pk>/report/", employee_api(community.ReportView, ["post"])),
]
