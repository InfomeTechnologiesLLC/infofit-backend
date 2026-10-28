from django.urls import path

from . import views

urlpatterns = [
    path("posts/", views.PostListView.as_view()),
    path("posts/photos/", views.PhotoPostView.as_view()),
    path("posts/assist/", views.PostAssistView.as_view()),
    path("posts/<int:pk>/", views.PostDetailView.as_view()),
    path("posts/<int:pk>/photo/", views.PostPhotoView.as_view()),
    path("posts/<int:pk>/comments/", views.CommentListView.as_view()),
    path("posts/<int:pk>/comments/<int:comment_id>/", views.CommentDetailView.as_view()),
    path("posts/<int:pk>/reactions/<str:kind>/", views.ReactionView.as_view()),
    path("posts/<int:pk>/report/", views.ReportView.as_view()),
]
