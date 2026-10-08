from django.urls import path
from . import views

urlpatterns = [
    path("source/", views.SourceView.as_view(), name="fitness_source"),
    path("sync/", views.SyncView.as_view(), name="fitness_sync"),
    path("home/", views.HomeView.as_view(), name="fitness_home"),
    path("steps/summary/", views.StepSummaryView.as_view(), name="fitness_step_summary"),
    path("activity/", views.ActivityView.as_view(), name="fitness_activity"),
    path("exercises/", views.ExerciseView.as_view(), name="fitness_exercises"),
    path("sleep/", views.SleepView.as_view(), name="fitness_sleep"),
]
