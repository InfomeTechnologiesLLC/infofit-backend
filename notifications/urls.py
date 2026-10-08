from django.urls import path

from .views import DeviceView, InboxView, ReadView

urlpatterns = [
    path("", InboxView.as_view()),
    path("<int:pk>/read/", ReadView.as_view()),
    path("devices/", DeviceView.as_view()),
]
