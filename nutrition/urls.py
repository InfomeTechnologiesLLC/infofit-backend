from django.urls import path

from .views import FrequentMealsView, MealDetailView, MealListCreateView, RepeatMealView

urlpatterns = [
    path("meals/", MealListCreateView.as_view()),
    path("meals/frequent/", FrequentMealsView.as_view()),
    path("meals/<int:meal_id>/", MealDetailView.as_view()),
    path("meals/<int:meal_id>/repeat/", RepeatMealView.as_view()),
]
