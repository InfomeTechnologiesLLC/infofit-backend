from django.urls import path

from . import views


urlpatterns = [
    path("programs/", views.ProgramListView.as_view()),
    path("programs/<int:pk>/join/", views.JoinProgramView.as_view()),
    path("programs/<int:pk>/check-in/", views.CheckInView.as_view()),
    path("hydration/today/", views.HydrationTodayView.as_view()),
    path("hydration/logs/", views.HydrationLogView.as_view()),
    path("hydration/logs/<int:pk>/", views.HydrationLogDetailView.as_view()),
    path("reminders/", views.ReminderSettingsView.as_view()),
]
