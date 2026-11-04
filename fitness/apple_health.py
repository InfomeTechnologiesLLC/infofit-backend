"""HealthySync authentication, connection lifecycle and transactional ingestion."""
import hashlib
import json
import secrets
from uuid import uuid4

from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.utils import timezone
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed, ValidationError

from accounts.models import User
from challenges.services import reconcile_user
from .apple_health_normalization import QUANTITIES, affected_dates, date_bounds, is_aggregate, normalize_days
from .models import AppleHealthConnection, AppleHealthRecord, AppleHealthSyncBatch, FitnessAccount, FitnessSource
from .views import Conflict, account_for


def token_digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


class AppleHealthAuthentication(BaseAuthentication):
    def authenticate(self, request):
        parts = get_authorization_header(request).split()
        if not parts:
            return None
        if len(parts) != 2 or parts[0].lower() != b"bearer":
            raise AuthenticationFailed("Use a HealthySync bearer token.")
        try:
            token = parts[1].decode("ascii")
        except UnicodeDecodeError:
            raise AuthenticationFailed("Invalid sync token.")
        if not token.startswith("ahs_") or len(token) > 128:
            raise AuthenticationFailed("Invalid sync token.")
        connection = AppleHealthConnection.objects.select_related("user").filter(token_hash=token_digest(token)).first()
        if connection is None or not connection.user.is_active:
            raise AuthenticationFailed("Invalid or revoked sync token.")
        return connection.user, connection

    def authenticate_header(self, request):
        return "Bearer"


@transaction.atomic
def issue_token(user, replace_existing=False, new_device=False, device_label="iPhone / HealthySync"):
    User.objects.select_for_update().get(pk=user.pk)
    account = account_for(user)
    account = FitnessAccount.objects.select_for_update().get(pk=account.pk)
    connection = AppleHealthConnection.objects.select_for_update().filter(user=user).first()
    desired_source = connection.source_id if connection and not new_device else None
    if account.active_source_id and account.active_source_id != desired_source and not replace_existing:
        raise Conflict("An active device exists. Set replace_existing to switch to HealthySync.")
    if desired_source is None:
        source = FitnessSource.objects.create(user=user, installation_id=uuid4(), kind="apple_health", device_label=device_label)
    else:
        source = connection.source
        source.device_label = device_label
        source.save(update_fields=["device_label"])
    token = "ahs_" + secrets.token_urlsafe(32)
    if connection is None:
        connection = AppleHealthConnection(user=user, source=source)
    connection.source = source
    connection.token_hash = token_digest(token)
    if new_device:
        connection.device_id = ""
    connection.save()
    changed = account.active_source_id != source.pk
    account.active_source = source
    if changed:
        records = list(AppleHealthRecord.objects.filter(source=source, first_date__isnull=False))
        if records:
            latest = max(records, key=lambda record: record.generated_at)
            account.time_zone = latest.time_zone
            dates = set().union(*(affected_dates(record) for record in records))
            normalize_days(source, dates, latest.generated_at, account.time_zone)
    account.save(update_fields=["active_source", "time_zone"])
    if changed:
        reconcile_user(user)
    return connection, token


@transaction.atomic
def revoke_token(user):
    User.objects.select_for_update().get(pk=user.pk)
    AppleHealthConnection.objects.filter(user=user).update(token_hash="", token_updated_at=timezone.now())


def record_dates(kind, data, zone):
    record_type = data["type"] if kind == "metric" else data["activity_type"]
    first, last = date_bounds(kind, record_type, data["start_at"], data["end_at"], zone)
    return record_type, first, last


def projection_dates(record):
    if record.kind == "workout" or record.record_type in QUANTITIES or record.record_type == "sleep_analysis":
        return affected_dates(record)
    return set()


@transaction.atomic
def ingest(user, authenticated_connection, data, payload_hash, device_header=None):
    user = User.objects.select_for_update().get(pk=user.pk)
    if not user.is_active:
        raise AuthenticationFailed("This account is inactive.")
    account = FitnessAccount.objects.select_for_update().get(user=user)
    connection = AppleHealthConnection.objects.select_for_update().get(pk=authenticated_connection.pk)
    if not connection.token_hash or not secrets.compare_digest(connection.token_hash, authenticated_connection.token_hash):
        raise AuthenticationFailed("Sync token was rotated or revoked.")
    if device_header is not None and device_header != data["device_id"]:
        raise ValidationError({"device_id": "X-Device-Id does not match the payload."})
    if connection.device_id and connection.device_id != data["device_id"]:
        raise Conflict("This token belongs to another device. Issue a token with new_device to switch phones.")
    source = connection.source
    if account.active_source_id != source.pk:
        raise Conflict("HealthySync is not the active fitness source.")
    prior = AppleHealthSyncBatch.objects.filter(source=source, export_id=data["export_id"]).first()
    if prior:
        if prior.payload_hash != payload_hash:
            raise Conflict("Export ID was already used with a different payload.")
        return prior.response
    if not connection.device_id:
        connection.device_id = data["device_id"]
        connection.save(update_fields=["device_id"])

    touched = set()
    duplicates = received = deleted = 0
    for key, kind in (("metrics", "metric"), ("workouts", "workout")):
        for item in data[key]:
            existing = AppleHealthRecord.objects.filter(source=source, kind=kind, record_id=item["id"]).first()
            payload = json.loads(json.dumps(item, cls=DjangoJSONEncoder))
            if existing:
                # HealthKit sample UUIDs are immutable and cannot be resurrected by a queued re-export.
                if existing.deleted or existing.generated_at > data["generated_at"]:
                    duplicates += 1
                    continue
                same = existing.payload == payload and existing.time_zone == data["timezone"]
                if same:
                    duplicates += 1
                    existing.generated_at = data["generated_at"]
                    existing.save(update_fields=["generated_at"])
                    if is_aggregate(existing):
                        touched.update(projection_dates(existing))
                    continue
                if existing.generated_at == data["generated_at"]:
                    raise Conflict("Conflicting versions of a record have the same generation time.")
                touched.update(projection_dates(existing))
            record_type, first, last = record_dates(kind, item, data["timezone"])
            record, _ = AppleHealthRecord.objects.update_or_create(source=source, kind=kind, record_id=item["id"],
                defaults={"record_type": record_type, "payload": payload, "start_at": item["start_at"],
                          "end_at": item["end_at"], "first_date": first, "last_date": last,
                          "time_zone": data["timezone"], "generated_at": data["generated_at"], "deleted": False})
            touched.update(projection_dates(record))
            received += 1
    for item in data["deletions"]:
        record, created = AppleHealthRecord.objects.get_or_create(source=source, kind=item["kind"], record_id=item["id"],
            defaults={"time_zone": data["timezone"], "generated_at": data["generated_at"], "deleted": True})
        if not created:
            if record.deleted:
                duplicates += 1
                continue
            touched.update(projection_dates(record))
            record.deleted = True
            record.generated_at = max(record.generated_at, data["generated_at"])
            record.save(update_fields=["deleted", "generated_at"])
            deleted += 1

    normalize_days(source, touched, data["generated_at"], data["timezone"])
    source.last_successful_sync_at = timezone.now()
    source.save(update_fields=["last_successful_sync_at"])
    # Older retry batches must not change the account's current time zone.
    latest = AppleHealthRecord.objects.filter(source=source).order_by("-generated_at", "-pk").first()
    account.time_zone = latest.time_zone if latest else data["timezone"]
    account.save(update_fields=["time_zone"])
    if touched:
        reconcile_user(user)
    result = {"ok": True, "received": received, "duplicates": duplicates, "deleted": deleted,
              "export_id": str(data["export_id"])}
    AppleHealthSyncBatch.objects.create(source=source, export_id=data["export_id"], payload_hash=payload_hash, response=result)
    return result
