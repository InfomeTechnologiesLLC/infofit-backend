import hashlib
import json
from datetime import date, timedelta
from backports.zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import Count, Sum
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import EmployeeProfile
from .models import DailyActivity, ExerciseRecord, FitnessAccount, FitnessSource, FitnessSyncBatch, SleepRecord
from .serializers import METRICS, SourceSerializer, SyncSerializer
from challenges.services import reconcile_user


class Conflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "Fitness source conflict."
    default_code = "conflict"


def account_for(user):
    account, _ = FitnessAccount.objects.get_or_create(user=user)
    return account


def source_data(account):
    source = account.active_source
    return {
        "installation_id": str(source.installation_id) if source else None,
        "device_label": source.device_label if source else None,
        "time_zone": account.time_zone,
        "last_sequence": source.last_sequence if source else 0,
        "last_successful_sync_at": source.last_successful_sync_at if source else None,
    }


class SourceView(APIView):
    def get(self, request):
        return Response(source_data(account_for(request.user)))

    def put(self, request):
        serializer = SourceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        with transaction.atomic():
            account = account_for(request.user)
            account = FitnessAccount.objects.select_for_update().select_related("active_source").get(pk=account.pk)
            active = account.active_source
            if active and active.installation_id != data["installation_id"] and not data["replace_existing"]:
                raise Conflict("An active device exists. Confirm a source switch explicitly.")
            source, _ = FitnessSource.objects.get_or_create(
                user=request.user, installation_id=data["installation_id"],
                defaults={"device_label": data.get("device_label", "")},
            )
            source.device_label = data.get("device_label", source.device_label)
            source.save(update_fields=["device_label"])
            account.active_source = source
            account.time_zone = data["time_zone"]
            account.save(update_fields=["active_source", "time_zone"])
            reconcile_user(request.user)
        return Response(source_data(account))


def replace_records(model, user, source, day, records):
    keep = []
    for record in records:
        keep.append(record["record_id"])
        fields = {
            "origin_package": record["origin_package"],
            "local_date": day,
            "start_at": record["start_at"],
            "end_at": record["end_at"],
        }
        if model is ExerciseRecord:
            fields["exercise_type"] = record.get("exercise_type", "unknown")
        model.objects.update_or_create(
            user=user, source=source, record_id=record["record_id"], defaults=fields
        )
    model.objects.filter(user=user, source=source, local_date=day).exclude(record_id__in=keep).delete()


class SyncView(APIView):
    def post(self, request):
        serializer = SyncSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        payload_hash = hashlib.sha256(json.dumps(request.data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with transaction.atomic():
            account = account_for(request.user)
            account = FitnessAccount.objects.select_for_update().select_related("active_source").get(pk=account.pk)
            source = account.active_source
            if source is None or source.installation_id != data["installation_id"]:
                raise Conflict("This device is not the active fitness source.")
            prior = FitnessSyncBatch.objects.filter(user=request.user, idempotency_key=data["idempotency_key"]).first()
            if prior:
                if prior.payload_hash != payload_hash:
                    raise Conflict("Idempotency key was used with a different payload.")
                return Response(prior.response)
            source = FitnessSource.objects.select_for_update().get(pk=source.pk)
            if data["sequence"] <= source.last_sequence:
                raise Conflict("Sync sequence is stale. Refresh source state and retry.")
            for day in data["days"]:
                values = {metric: day[metric] for metric in METRICS}
                DailyActivity.objects.update_or_create(
                    user=request.user, local_date=day["local_date"],
                    defaults={"source": source, "time_zone": data["time_zone"],
                              "observed_at": day["observed_at"],
                              "metric_status": day["metric_status"], **values},
                )
                if day["metric_status"]["exercise_minutes"] in ("available", "no_data"):
                    replace_records(ExerciseRecord, request.user, source, day["local_date"], day["exercises"])
                if day["metric_status"]["sleep_minutes"] in ("available", "no_data"):
                    replace_records(SleepRecord, request.user, source, day["local_date"], day["sleep"])
            source.last_sequence = data["sequence"]
            source.last_successful_sync_at = timezone.now()
            source.save(update_fields=["last_sequence", "last_successful_sync_at"])
            account.time_zone = data["time_zone"]
            account.save(update_fields=["time_zone"])
            result = {"accepted_dates": [day["local_date"].isoformat() for day in data["days"]],
                      "last_sequence": source.last_sequence}
            FitnessSyncBatch.objects.create(user=request.user, source=source,
                idempotency_key=data["idempotency_key"], payload_hash=payload_hash,
                sequence=data["sequence"], response=result)
            reconcile_user(request.user)
        return Response(result)


def day_payload(day):
    return {"date": day.local_date, "time_zone": day.time_zone,
            "metrics": {metric: getattr(day, metric) for metric in METRICS},
            "metric_status": day.metric_status, "last_synced_at": day.synced_at}


def step_estimates(day):
    """Display-only rough values; never stored or used for rewards."""
    if not day or day.metric_status.get("steps") != "available" or day.steps is None:
        return {"distance_m": None, "active_kcal": None}
    return {
        "distance_m": round(day.steps * 0.75, 1) if day.distance_m is None else None,
        "active_kcal": round(day.steps * 0.04, 1) if day.active_kcal is None else None,
    }


class HomeView(APIView):
    def get(self, request):
        account = account_for(request.user)
        zone = ZoneInfo(account.time_zone)
        requested = request.query_params.get("date")
        if requested:
            from datetime import date
            try:
                local_date = date.fromisoformat(requested)
            except ValueError:
                raise ValidationError({"date": "Use YYYY-MM-DD."})
        else:
            local_date = timezone.now().astimezone(zone).date()
        day = DailyActivity.objects.filter(user=request.user, source=account.active_source,
                                           local_date=local_date).first() if account.active_source else None
        profile, _ = EmployeeProfile.objects.get_or_create(user=request.user)
        data = day_payload(day) if day else {
            "date": local_date, "time_zone": account.time_zone,
            "metrics": {metric: None for metric in METRICS},
            "metric_status": {metric: "no_data" for metric in METRICS},
            "last_synced_at": None,
        }
        data.update({"step_goal": profile.daily_step_goal,
                     "source": "health_connect" if day else None,
                     "step_estimates": step_estimates(day),
                     "estimate_note": "Rough step-based estimate (0.75 m and 0.04 kcal per step); actual values may differ. Never used for rewards.",
                     "last_successful_sync_at": account.active_source.last_successful_sync_at if account.active_source else None})
        return Response(data)


class StepSummaryView(APIView):
    def get(self, request):
        period = request.query_params.get("period", "week")
        if period not in ("week", "month"):
            raise ValidationError({"period": "Use week or month."})
        account = account_for(request.user)
        today = timezone.now().astimezone(ZoneInfo(account.time_zone)).date()
        start = today - timedelta(days=today.weekday()) if period == "week" else date(today.year, today.month, 1)
        rows = DailyActivity.objects.filter(
            user=request.user, source=account.active_source,
            local_date__gte=start, local_date__lte=today,
            metric_status__steps="available", steps__isnull=False,
        ) if account.active_source else DailyActivity.objects.none()
        result = rows.aggregate(total=Sum("steps"), days=Count("id"))
        return Response({"period": period, "from": start, "to": today,
                         "steps": result["total"], "days_with_data": result["days"],
                         "days_elapsed": (today - start).days + 1,
                         "note": "Health Connect step data only. Missing days are not counted as zero."})


def date_range(request):
    from datetime import date
    try:
        start = date.fromisoformat(request.query_params["from"])
        end = date.fromisoformat(request.query_params["to"])
    except (KeyError, ValueError):
        raise ValidationError("Provide from and to as YYYY-MM-DD.")
    if end < start or end - start >= timedelta(days=31):
        raise ValidationError("Date range must be at most 31 days.")
    return start, end


class ActivityView(APIView):
    def get(self, request):
        start, end = date_range(request)
        account = account_for(request.user)
        rows = DailyActivity.objects.filter(user=request.user, source=account.active_source,
            local_date__range=(start, end)).order_by("local_date") if account.active_source else []
        return Response([day_payload(row) for row in rows])


class RecordView(APIView):
    model = None

    def get(self, request):
        start, end = date_range(request)
        account = account_for(request.user)
        rows = self.model.objects.filter(user=request.user, source=account.active_source,
            local_date__range=(start, end)).order_by("-start_at")[:200] if account.active_source else []
        fields = ("record_id", "origin_package", "local_date", "start_at", "end_at")
        return Response([{**{field: getattr(row, field) for field in fields},
                          **({"exercise_type": row.exercise_type} if self.model is ExerciseRecord else {})}
                         for row in rows])


class ExerciseView(RecordView):
    model = ExerciseRecord


class SleepView(RecordView):
    model = SleepRecord
