from django.urls import path

from . import views

urlpatterns = [
    path("message/", views.AssistantMessageView.as_view()),  # older app versions
    path("chat/", views.AgentChatView.as_view()),
    path("stream/", views.AgentStreamView.as_view()),
    path("history/", views.ChatHistoryView.as_view()),
    path("preferences/", views.PreferencesView.as_view()),
    path("proposals/<int:pk>/confirm/", views.ProposalConfirmView.as_view()),
    path("proposals/<int:pk>/cancel/", views.ProposalCancelView.as_view()),
    path("proposals/<int:pk>/undo/", views.ProposalUndoView.as_view()),
]
