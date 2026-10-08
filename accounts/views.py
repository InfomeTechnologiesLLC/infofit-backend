import logging

from rest_framework import status
from rest_framework.generics import GenericAPIView, RetrieveUpdateAPIView
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .models import EmployeeProfile
from .serializers import LoginSerializer, LogoutSerializer, PasswordChangeSerializer, ProfileSerializer

audit = logging.getLogger("infome.audit")


class LoginView(TokenObtainPairView):
    serializer_class = LoginSerializer
    permission_classes = []
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"


class RefreshView(TokenRefreshView):
    permission_classes = []
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"


class LogoutView(GenericAPIView):
    serializer_class = LogoutSerializer

    def post(self, request):
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            RefreshToken(ser.validated_data["refresh"]).blacklist()
        except TokenError:
            return Response(
                {"error": {"code": "invalid_token", "message": "Token is invalid or already revoked.", "details": None}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        audit.info("logout user=%s", request.user.id)
        return Response(status=status.HTTP_204_NO_CONTENT)


class ProfileView(RetrieveUpdateAPIView):
    """Each user can only ever see/edit their OWN profile (no id in the URL)."""

    serializer_class = ProfileSerializer
    http_method_names = ["get", "put", "patch", "head", "options"]

    def get_object(self):
        profile, _ = EmployeeProfile.objects.select_related("user", "department", "team").get_or_create(user=self.request.user)
        return profile


class PasswordChangeView(GenericAPIView):
    serializer_class = PasswordChangeSerializer

    def post(self, request):
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        request.user.set_password(ser.validated_data["new_password"])
        request.user.save(update_fields=["password"])
        audit.info("password_change user=%s", request.user.id)
        return Response(status=status.HTTP_204_NO_CONTENT)
