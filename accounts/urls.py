from django.urls import path

from . import views

urlpatterns = [
    path("auth/register/", views.RegisterView.as_view(), name="register"),
    path("auth/departments/", views.DepartmentListView.as_view(), name="departments"),
    path("auth/login/", views.LoginView.as_view(), name="login"),
    path("auth/refresh/", views.RefreshView.as_view(), name="token_refresh"),
    path("auth/logout/", views.LogoutView.as_view(), name="logout"),
    path("auth/password/change/", views.PasswordChangeView.as_view(), name="password_change"),
    path("profile/", views.ProfileView.as_view(), name="profile"),
    path("profile/onboarding/", views.OnboardingView.as_view(), name="profile_onboarding"),
]
