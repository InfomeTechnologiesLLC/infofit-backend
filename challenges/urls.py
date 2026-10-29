from django.urls import path
from . import views

urlpatterns = [
    path("", views.ChallengeListView.as_view()),
    path("me/", views.MyChallengesView.as_view()),
    path("leaderboard/", views.LeaderboardView.as_view()),
    path("rankings/", views.RankingsView.as_view()),
    path("<int:pk>/join/", views.JoinView.as_view()),
]
