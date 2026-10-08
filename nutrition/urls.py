from django.urls import path

from .views import MealDetailView, MealListCreateView

urlpatterns = [
    path("meals/", MealListCreateView.as_view()),
    path("meals/<int:meal_id>/", MealDetailView.as_view()),
]
