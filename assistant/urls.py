from django.urls import path

from . import views

urlpatterns = [
    path("message/", views.AssistantMessageView.as_view()),
    path("proposals/<int:pk>/confirm/", views.ProposalConfirmView.as_view()),
    path("proposals/<int:pk>/cancel/", views.ProposalCancelView.as_view()),
    path("proposals/<int:pk>/undo/", views.ProposalUndoView.as_view()),
]
