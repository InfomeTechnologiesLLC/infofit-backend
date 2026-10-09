from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone
from rest_framework import status
from rest_framework import serializers
from rest_framework.exceptions import APIException, NotFound, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import EmployeeProfile
from challenges.views import local_today
from fitness.models import FitnessAccount
from .models import HydrationLog, ProgramCheckIn, ProgramEnrollment, ReminderSettings, WellnessProgram


def user_goal(user):
    profile = EmployeeProfile.objects.filter(user=user).first()
    return profile.fitness_goal if profile else EmployeeProfile.FitnessGoal.GENERAL


def eligible_program(pk, user, today):
    program = WellnessProgram.objects.filter(pk=pk, is_published=True, end_date__gte=today).first()
    if not program or (program.focus_goal not in ("all", user_goal(user)) and
                       not ProgramEnrollment.objects.filter(program=program, user=user).exists()):
        raise NotFound("Program not found.")
    return program


def program_payload(program, enrollment, today, goal):
    return {
        "id": program.pk,
        "title": program.title,
        "summary": program.summary,
        "daily_action": program.daily_action,
        "focus_goal": program.focus_goal,
        "recommended": program.focus_goal == goal,
        "start_date": program.start_date,
        "end_date": program.end_date,
        "joined": enrollment is not None,
        "can_join": program.start_date <= today <= program.end_date,
        "checked_in_today": bool(enrollment and enrollment.today_count),
        "check_in_count": enrollment.check_in_count if enrollment else 0,
    }


def enrollments_for(user, programs, today):
    return {
        enrollment.program_id: enrollment
        for enrollment in ProgramEnrollment.objects.filter(user=user, program__in=programs)
        .annotate(check_in_count=Count("check_ins"),
                  today_count=Count("check_ins", filter=Q(check_ins__local_date=today)))
    }


class ProgramListView(APIView):
    def get(self, request):
        today = local_today(request.user)
        goal = user_goal(request.user)
        programs = list(WellnessProgram.objects.filter(is_published=True, end_date__gte=today)
            .filter(Q(focus_goal__in=["all", goal]) | Q(enrollments__user=request.user))
            .distinct().order_by("start_date", "pk")[:100])
        enrollments = enrollments_for(request.user, programs, today)
        programs.sort(key=lambda item: (item.focus_goal != goal, item.start_date, item.pk))
        return Response([program_payload(item, enrollments.get(item.pk), today, goal) for item in programs])


class JoinProgramView(APIView):
    def post(self, request, pk):
        today = local_today(request.user)
        program = eligible_program(pk, request.user, today)
        if not program.start_date <= today <= program.end_date:
            raise ValidationError("This program is not open for joining today.")
        with transaction.atomic():
            enrollment, created = ProgramEnrollment.objects.get_or_create(program=program, user=request.user)
        enrollment.check_in_count = ProgramCheckIn.objects.filter(enrollment=enrollment).count()
        enrollment.today_count = int(ProgramCheckIn.objects.filter(enrollment=enrollment, local_date=today).exists())
        return Response(program_payload(program, enrollment, today, user_goal(request.user)),
                        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class CheckInView(APIView):
    def post(self, request, pk):
        today = local_today(request.user)
        program = eligible_program(pk, request.user, today)
        if not program.start_date <= today <= program.end_date:
            raise ValidationError("This program is not open for check-ins today.")
        enrollment = ProgramEnrollment.objects.filter(program=program, user=request.user).first()
        if not enrollment:
            raise ValidationError("Join this program before checking in.")
        with transaction.atomic():
            _, created = ProgramCheckIn.objects.get_or_create(enrollment=enrollment, local_date=today)
        enrollment.check_in_count = ProgramCheckIn.objects.filter(enrollment=enrollment).count()
        enrollment.today_count = 1
        return Response(program_payload(program, enrollment, today, user_goal(request.user)),
                        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class HydrationConflict(APIException):
    status_code = 409
    default_detail = "This water log request ID was already used for a different amount."


class HydrationWriteSerializer(serializers.Serializer):
    client_request_id = serializers.UUIDField()
    volume_ml = serializers.IntegerField(min_value=50, max_value=1000)


class ReminderWriteSerializer(serializers.Serializer):
    water_enabled = serializers.BooleanField(required=False)
    movement_enabled = serializers.BooleanField(required=False)
    water_interval_minutes = serializers.IntegerField(required=False, min_value=30, max_value=240)
    movement_interval_minutes = serializers.IntegerField(required=False, min_value=30, max_value=240)
    start_hour = serializers.IntegerField(required=False, min_value=0, max_value=23)
    end_hour = serializers.IntegerField(required=False, min_value=1, max_value=24)
    time_zone = serializers.CharField(required=False, max_length=64)

    def validate_time_zone(self, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise serializers.ValidationError("Use a valid IANA time zone.") from exc
        return value

    def validate(self, data):
        instance = self.instance
        start = data.get("start_hour", instance.start_hour if instance else 9)
        end = data.get("end_hour", instance.end_hour if instance else 19)
        if start >= end:
            raise serializers.ValidationError({"end_hour": "End hour must be later than start hour."})
        return data


def employee_time_zone(user):
    value = ReminderSettings.objects.filter(user=user).values_list("time_zone", flat=True).first()
    if not value:
        value = FitnessAccount.objects.filter(user=user).values_list("time_zone", flat=True).first()
    try:
        return value or "UTC", ZoneInfo(value or "UTC")
    except ZoneInfoNotFoundError:
        return "UTC", ZoneInfo("UTC")


def hydration_today(user):
    zone_name, zone = employee_time_zone(user)
    today = timezone.now().astimezone(zone).date()
    logs = HydrationLog.objects.filter(user=user, local_date=today)
    aggregate = logs.aggregate(total_ml=Sum("volume_ml"), entries=Count("id"))
    return {"local_date": today, "time_zone": zone_name,
            "total_ml": aggregate["total_ml"] or 0, "entries": aggregate["entries"],
            "last_log_id": logs.order_by("-created_at", "-pk").values_list("pk", flat=True).first()}


class HydrationTodayView(APIView):
    def get(self, request):
        response = hydration_today(request.user)
        request_id = request.query_params.get("client_request_id")
        if request_id:
            serializer = serializers.UUIDField()
            try:
                parsed = serializer.run_validation(request_id)
            except serializers.ValidationError as exc:
                raise ValidationError({"client_request_id": "Provide a valid request ID."}) from exc
            response["request_seen"] = HydrationLog.objects.filter(
                user=request.user, client_request_id=parsed
            ).exists()
        return Response(response)


class HydrationLogView(APIView):
    def post(self, request):
        serializer = HydrationWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        zone_name, zone = employee_time_zone(request.user)
        today = timezone.now().astimezone(zone).date()
        with transaction.atomic():
            log, created = HydrationLog.objects.get_or_create(
                user=request.user, client_request_id=data["client_request_id"],
                defaults={"volume_ml": data["volume_ml"], "local_date": today, "time_zone": zone_name},
            )
            if not created and log.volume_ml != data["volume_ml"]:
                raise HydrationConflict()
        response = hydration_today(request.user)
        response["log_id"] = log.pk
        return Response(response, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class HydrationLogDetailView(APIView):
    def delete(self, request, pk):
        log = HydrationLog.objects.filter(user=request.user, pk=pk).first()
        if not log:
            raise NotFound("Water log not found.")
        log.delete()
        return Response(hydration_today(request.user))


def reminder_payload(settings):
    return {"water_enabled": settings.water_enabled, "movement_enabled": settings.movement_enabled,
            "water_interval_minutes": settings.water_interval_minutes,
            "movement_interval_minutes": settings.movement_interval_minutes,
            "start_hour": settings.start_hour, "end_hour": settings.end_hour,
            "time_zone": settings.time_zone}


class ReminderSettingsView(APIView):
    def get_settings(self, user):
        zone_name, _ = employee_time_zone(user)
        settings, _ = ReminderSettings.objects.get_or_create(user=user, defaults={"time_zone": zone_name})
        return settings

    def get(self, request):
        return Response(reminder_payload(self.get_settings(request.user)))

    def patch(self, request):
        settings = self.get_settings(request.user)
        serializer = ReminderWriteSerializer(settings, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        for field, value in serializer.validated_data.items():
            setattr(settings, field, value)
        settings.save()
        return Response(reminder_payload(settings))
