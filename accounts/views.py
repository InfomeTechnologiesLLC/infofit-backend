import logging

from django.utils import timezone

from rest_framework import status
from rest_framework.generics import GenericAPIView, RetrieveUpdateAPIView
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from django.conf import settings
from django.db import IntegrityError, transaction
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.views import APIView

from .models import Department, EmployeeProfile, Role, User
from .serializers import LoginSerializer, LogoutSerializer, PasswordChangeSerializer, ProfileSerializer, RegisterSerializer

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


class OnboardingView(GenericAPIView):
    """POST marks the first-login tutorial done (idempotent); DELETE re-arms it so it can be replayed."""
    serializer_class = ProfileSerializer

    def _profile(self, request):
        profile, _ = EmployeeProfile.objects.select_related("user", "department", "team").get_or_create(user=request.user)
        return profile

    def post(self, request):
        profile = self._profile(request)
        if profile.onboarding_completed_at is None:
            profile.onboarding_completed_at = timezone.now()
            profile.save(update_fields=["onboarding_completed_at", "updated_at"])
        return Response(ProfileSerializer(profile).data)

    def delete(self, request):
        profile = self._profile(request)
        profile.onboarding_completed_at = None
        profile.save(update_fields=["onboarding_completed_at", "updated_at"])
        return Response(ProfileSerializer(profile).data)


class PasswordChangeView(GenericAPIView):
    serializer_class = PasswordChangeSerializer

    def post(self, request):
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        request.user.set_password(ser.validated_data["new_password"])
        request.user.save(update_fields=["password"])
        audit.info("password_change user=%s", request.user.id)
        return Response(status=status.HTTP_204_NO_CONTENT)


class RegistrationClosed(PermissionDenied):
    default_detail = "Sign-up is turned off. Please ask HR for an account."
    default_code = "registration_closed"


class DepartmentListView(APIView):
    """Departments for the sign-up form (names only), and whether sign-up is open."""
    permission_classes = []
    authentication_classes = []

    def get(self, request):
        return Response({
            "registration_open": settings.ALLOW_SELF_REGISTRATION,
            "results": list(Department.objects.order_by("name").values("id", "name")),
        })


class RegisterView(GenericAPIView):
    """Creates an ordinary employee account and signs it in (same response as login).

    The role is always Employee: staff and admin roles are only ever given in the staff portal.
    """
    serializer_class = RegisterSerializer
    permission_classes = []
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "register"

    def post(self, request):
        if not settings.ALLOW_SELF_REGISTRATION:
            raise RegistrationClosed()
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        try:
            with transaction.atomic():
                user = User.objects.create_user(email=data["email"], password=data["password"], role=Role.EMPLOYEE,
                                                first_name=data["first_name"], last_name=data["last_name"])
                profile, _ = EmployeeProfile.objects.get_or_create(user=user)
                profile.department = data.get("department")
                profile.save(update_fields=["department", "updated_at"])
        except IntegrityError:  # two sign-ups with the same email at the same moment
            raise ValidationError({"email": "An account with this email already exists. Sign in instead."})
        refresh = RefreshToken.for_user(user)
        audit.info("self-registration user=%s department=%s", user.id, profile.department_id)
        return Response({
            "access": str(refresh.access_token),
            "refresh": str(refresh),
            "user": {"id": user.id, "email": user.email, "full_name": user.full_name, "role": user.role},
        }, status=status.HTTP_201_CREATED)
