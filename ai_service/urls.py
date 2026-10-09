from django.urls import path

from .views import ChatView, CoachingView, FoodSearchView, LensView, MealGuidanceView

urlpatterns = [
    path("chat/", ChatView.as_view()),
    path("lens/", LensView.as_view()),
    path("foods/", FoodSearchView.as_view()),
    path("coaching/", CoachingView.as_view()),
    path("meal-guidance/", MealGuidanceView.as_view()),
]
