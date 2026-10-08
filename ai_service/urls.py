from django.urls import path

from .views import ChatView, FoodSearchView, LensView

urlpatterns = [
    path("chat/", ChatView.as_view()),
    path("lens/", LensView.as_view()),
    path("foods/", FoodSearchView.as_view()),
]
