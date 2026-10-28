try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # Python 3.8 hosts
    from backports.zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

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
from .models import BodyMeasurement, bmi_category, HydrationLog, ProgramCheckIn, ProgramEnrollment, ReminderSettings, WellnessProgram


def user_goal(user):
    profile = EmployeeProfile.objects.filter(user=user).first()
    return profile.fitness_goal if profile else EmployeeProfile.FitnessGoal.GENERAL


def visible_programs(user):
    """Company programs plus this employee's own personal plans; never someone else's plan."""
    return WellnessProgram.objects.filter(is_published=True).filter(Q(owner__isnull=True) | Q(owner=user))


def eligible_program(pk, user, today):
    program = visible_programs(user).filter(pk=pk, end_date__gte=today).first()
    if not program or (program.owner_id is None and program.focus_goal not in ("all", user_goal(user)) and
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
        "personal": program.owner_id is not None,
        "today_action": program.action_for(today),
        "plan_days": program.plan_days or [],
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
        programs = list(visible_programs(request.user).filter(end_date__gte=today)
            .filter(Q(focus_goal__in=["all", goal]) | Q(enrollments__user=request.user) | Q(owner=request.user))
            .distinct().order_by("start_date", "pk")[:100])
        enrollments = enrollments_for(request.user, programs, today)
        programs.sort(key=lambda item: (item.owner_id is None, item.focus_goal != goal, item.start_date, item.pk))
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
    challenge_enabled = serializers.BooleanField(required=False)
    bmi_enabled = serializers.BooleanField(required=False)
    water_interval_minutes = serializers.IntegerField(required=False, min_value=15, max_value=360)
    movement_interval_minutes = serializers.IntegerField(required=False, min_value=15, max_value=360)
    # Older app versions send whole hours; newer versions send minutes after midnight.
    start_hour = serializers.IntegerField(required=False, min_value=0, max_value=23)
    end_hour = serializers.IntegerField(required=False, min_value=1, max_value=24)
    start_minute = serializers.IntegerField(required=False, min_value=0, max_value=1439)
    end_minute = serializers.IntegerField(required=False, min_value=1, max_value=1440)
    challenge_minute = serializers.IntegerField(required=False, min_value=0, max_value=1439)
    time_zone = serializers.CharField(required=False, max_length=64)

    def validate_time_zone(self, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise serializers.ValidationError("Use a valid IANA time zone.") from exc
        return value

    def validate(self, data):
        instance = self.instance
        if "start_minute" not in data and "start_hour" in data:
            data["start_minute"] = data["start_hour"] * 60
        if "end_minute" not in data and "end_hour" in data:
            data["end_minute"] = data["end_hour"] * 60
        data.pop("start_hour", None)
        data.pop("end_hour", None)
        start = data.get("start_minute", instance.start_minute if instance else 540)
        end = data.get("end_minute", instance.end_minute if instance else 1140)
        if start >= end:
            raise serializers.ValidationError({"end_minute": "End time must be later than start time."})
        if "start_minute" in data:
            data["start_hour"] = data["start_minute"] // 60
        if "end_minute" in data:
            data["end_hour"] = min(24, max(1, -(-data["end_minute"] // 60)))
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
    last = logs.order_by("-created_at", "-pk").values("pk", "created_at").first()
    return {"local_date": today, "time_zone": zone_name,
            "total_ml": aggregate["total_ml"] or 0, "entries": aggregate["entries"],
            "last_log_id": last["pk"] if last else None,
            "last_logged_at": last["created_at"] if last else None}


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
            "challenge_enabled": settings.challenge_enabled,
            "bmi_enabled": settings.bmi_enabled,
            "water_interval_minutes": settings.water_interval_minutes,
            "movement_interval_minutes": settings.movement_interval_minutes,
            "start_hour": settings.start_hour, "end_hour": settings.end_hour,
            "start_minute": settings.start_minute, "end_minute": settings.end_minute,
            "challenge_minute": settings.challenge_minute,
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


# ---- BMI tracker -----------------------------------------------------------------------------

class BodyMeasurementSerializer(serializers.Serializer):
    height_cm = serializers.DecimalField(max_digits=4, decimal_places=1, min_value=Decimal("100"), max_value=Decimal("250"))
    weight_kg = serializers.DecimalField(max_digits=5, decimal_places=1, min_value=Decimal("25"), max_value=Decimal("300"))


def compute_bmi(height_cm, weight_kg):
    metres = Decimal(height_cm) / Decimal(100)
    return (Decimal(weight_kg) / (metres * metres)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def _entry(m):
    return {"id": m.pk, "local_date": m.local_date, "height_cm": float(m.height_cm),
            "weight_kg": float(m.weight_kg), "bmi": float(m.bmi), "category": bmi_category(float(m.bmi))}


def bmi_payload(user):
    """Latest value, due date, and one point per week (the last entry of each Monday-start week)."""
    _, zone = employee_time_zone(user)
    today = timezone.now().astimezone(zone).date()
    rows = list(BodyMeasurement.objects.filter(user=user).order_by("local_date")[:520])
    weeks = {}
    for m in rows:
        weeks[m.local_date - timedelta(days=m.local_date.weekday())] = m  # later rows overwrite earlier
    weekly, previous = [], None
    for week_start in sorted(weeks):
        item = _entry(weeks[week_start])
        item["week_start"] = week_start
        item["weight_change_kg"] = round(item["weight_kg"] - previous["weight_kg"], 1) if previous else None
        item["bmi_change"] = round(item["bmi"] - previous["bmi"], 1) if previous else None
        weekly.append(item)
        previous = item
    latest = _entry(rows[-1]) if rows else None
    next_due = rows[-1].local_date + timedelta(days=7) if rows else today
    return {
        "local_date": today, "latest": latest, "height_cm": latest["height_cm"] if latest else None,
        "first_weight_kg": float(rows[0].weight_kg) if rows else None,
        "total_weight_change_kg": round(latest["weight_kg"] - float(rows[0].weight_kg), 1) if rows else None,
        "next_due_date": next_due, "update_due": today >= next_due,
        "weekly": weekly[-52:],
        "note": "BMI is a general screening figure and does not account for muscle, age or body build. "
                "It is not a diagnosis. Talk to a clinician about any health concern.",
    }


class BmiView(APIView):
    def get(self, request):
        return Response(bmi_payload(request.user))

    def post(self, request):
        serializer = BodyMeasurementSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        zone_name, zone = employee_time_zone(request.user)
        today = timezone.now().astimezone(zone).date()
        _, created = BodyMeasurement.objects.update_or_create(
            user=request.user, local_date=today,
            defaults={"height_cm": data["height_cm"], "weight_kg": data["weight_kg"],
                      "bmi": compute_bmi(data["height_cm"], data["weight_kg"]), "time_zone": zone_name},
        )
        return Response(bmi_payload(request.user), status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class BmiDetailView(APIView):
    def delete(self, request, pk):
        entry = BodyMeasurement.objects.filter(user=request.user, pk=pk).first()
        if not entry:
            raise NotFound("Measurement not found.")
        entry.delete()
        return Response(bmi_payload(request.user))
