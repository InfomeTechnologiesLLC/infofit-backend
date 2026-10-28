from django.urls import path

from . import views

urlpatterns = [
    path("settings/", views.AutomationSettingsView.as_view()),
    path("step-goal/", views.StepGoalSuggestionView.as_view()),
    path("step-goal/<int:pk>/<str:action>/", views.StepGoalActionView.as_view()),
    path("challenge-suggestions/", views.ChallengeSuggestionsView.as_view()),
    path("reminder-timing/", views.ReminderTimingView.as_view()),
    path("hr-digest/", views.HrDigestView.as_view()),
]
