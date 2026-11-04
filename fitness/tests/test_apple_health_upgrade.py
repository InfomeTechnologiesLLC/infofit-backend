"""Exercise the real 0002 -> 0003 migration with a populated, active Android account."""
import hashlib
import json
from datetime import date, datetime, timezone
from uuid import uuid4

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import User
from challenges.models import Challenge, ChallengeEnrollment, ChallengeProgress, PointsLedger
from fitness.models import DailyActivity, ExerciseRecord, FitnessAccount, FitnessSource, FitnessSyncBatch


class AppleHealthUpgradeTests(TransactionTestCase):
    def restore_schema(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_existing_data_tokens_retries_and_next_sync_survive_upgrade(self):
        self.addCleanup(self.restore_schema)
        executor = MigrationExecutor(connection)
        previous = [("fitness", "0002_manual_entries_and_recording_method")]
        executor.migrate(previous)
        old = executor.loader.project_state(previous).apps
        Source = old.get_model("fitness", "FitnessSource")
        Account = old.get_model("fitness", "FitnessAccount")
        Day = old.get_model("fitness", "DailyActivity")
        Exercise = old.get_model("fitness", "ExerciseRecord")
        Sleep = old.get_model("fitness", "SleepRecord")
        Batch = old.get_model("fitness", "FitnessSyncBatch")

        user = User.objects.create_user(email="existing-phone@example.test", password="existing-password")
        refresh = RefreshToken.for_user(user)
        access_token, refresh_token = str(refresh.access_token), str(refresh)
        observed = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
        source = Source.objects.create(user_id=user.pk, installation_id=uuid4(), device_label="Existing Pixel",
                                       last_sequence=7, last_successful_sync_at=observed)
        Account.objects.create(user_id=user.pk, active_source_id=source.pk, time_zone="UTC")
        Day.objects.create(user_id=user.pk, source_id=source.pk, local_date=date(2026, 9, 30), time_zone="UTC",
                           steps=8000, manual_steps=500, distance_m=4500, active_kcal=250,
                           exercise_minutes=30, sleep_minutes=420, observed_at=observed,
                           metric_status={metric: "available" for metric in
                                          ("steps", "distance_m", "active_kcal", "exercise_minutes", "sleep_minutes")})
        Exercise.objects.create(user_id=user.pk, source_id=source.pk, record_id="existing-workout", origin_package="watch",
                                local_date=date(2026, 9, 30), start_at="2026-09-30T08:00:00Z", end_at="2026-09-30T08:30:00Z",
                                exercise_type="running", recording_method="automatic")
        Sleep.objects.create(user_id=user.pk, source_id=source.pk, record_id="existing-sleep", origin_package="watch",
                             local_date=date(2026, 9, 30), start_at="2026-09-30T00:00:00Z", end_at="2026-09-30T07:00:00Z",
                             recording_method="automatic")
        day_body = {"local_date": "2026-09-30", "observed_at": "2026-10-01T12:00:00Z", "steps": 8000,
                    "metric_status": {"steps": "available", "distance_m": "no_data", "active_kcal": "no_data",
                                      "exercise_minutes": "permission_unavailable", "sleep_minutes": "permission_unavailable"},
                    "exercises": [], "sleep": []}
        body = {"installation_id": str(source.installation_id), "idempotency_key": str(uuid4()), "sequence": 7,
                "time_zone": "UTC", "days": [day_body]}
        cached_response = {"accepted_dates": ["2026-09-30"], "last_sequence": 7}
        Batch.objects.create(user_id=user.pk, source_id=source.pk, sequence=7, idempotency_key=body["idempotency_key"],
                             payload_hash=hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                             response=cached_response)
        challenge = Challenge.objects.create(slug="existing-reward", title="Existing challenge", target_steps=7000,
                                             start_date=date(2026, 9, 30), end_date=date(2026, 9, 30), reward_points=25)
        enrollment = ChallengeEnrollment.objects.create(user=user, challenge=challenge, joined_local_date=date(2026, 9, 30))
        ChallengeProgress.objects.create(enrollment=enrollment, revision=1, is_complete=True, verified_steps=7500,
                                         has_step_data=True, progress_value=7500)
        PointsLedger.objects.create(user=user, enrollment=enrollment, revision=1, delta=25, reason="award")
        snapshots = {model: list(model.objects.values()) for model in (Source, Account, Day, Exercise, Sleep, Batch)}

        self.restore_schema()

        # Querying through the old ORM verifies every existing column, ID and timestamp is unchanged.
        for model, before in snapshots.items():
            self.assertEqual(list(model.objects.values()), before, model.__name__)
        self.assertEqual(list(PointsLedger.objects.values_list("delta", flat=True)), [25])
        self.assertEqual(FitnessSource.objects.get(pk=source.pk).kind, "health_connect")
        self.assertEqual(DailyActivity.objects.get(user=user).verified_metrics, {})
        self.assertEqual(DailyActivity.objects.get(user=user).verified_value("steps"), 7500)
        self.assertIsNone(ExerciseRecord.objects.get(user=user).duration_seconds)
        self.assertEqual(FitnessAccount.objects.get(user=user).active_source_id, source.pk)

        # After the maintenance window, ordinary ORM inserts supply the defaults.
        new_source = FitnessSource.objects.create(user_id=user.pk, installation_id=uuid4(), device_label="New worker insert")
        new_day = DailyActivity.objects.create(user_id=user.pk, source_id=source.pk, local_date=date(2026, 9, 29),
                                              time_zone="UTC", steps=100, observed_at=observed,
                                              metric_status={"steps": "available"})
        self.assertEqual(FitnessSource.objects.get(pk=new_source.pk).kind, "health_connect")
        self.assertEqual(DailyActivity.objects.get(pk=new_day.pk).verified_metrics, {})

        if (connection.vendor == "mysql" and not connection.mysql_is_mariadb
                and connection.mysql_version < (8, 0, 13)):
            # MySQL 5.7 must not receive an unsupported permanent JSON default.
            with connection.cursor() as cursor:
                cursor.execute("SELECT IS_NULLABLE, COLUMN_DEFAULT FROM information_schema.COLUMNS "
                               "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
                               [DailyActivity._meta.db_table, "verified_metrics"])
                self.assertEqual(cursor.fetchone(), ("NO", None))
        else:
            # Databases supporting permanent defaults retain the old-worker behavior.
            old_source = Source.objects.create(user_id=user.pk, installation_id=uuid4(), device_label="Old worker insert")
            old_day = Day.objects.create(user_id=user.pk, source_id=source.pk, local_date=date(2026, 9, 28), time_zone="UTC",
                                         steps=100, observed_at=observed, metric_status={"steps": "available"})
            self.assertEqual(FitnessSource.objects.get(pk=old_source.pk).kind, "health_connect")
            self.assertEqual(DailyActivity.objects.get(pk=old_day.pk).verified_metrics, {})

        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION="Bearer " + access_token)
        home = api.get("/api/v1/fitness/home/", {"date": "2026-09-30"})
        self.assertEqual(home.status_code, 200)
        self.assertEqual(home.data["source"], "health_connect")
        self.assertEqual(home.data["metrics"]["steps"], 8000)
        retry = api.post("/api/v1/fitness/sync/", body, format="json")
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.data, cached_response)
        self.assertEqual(FitnessSyncBatch.objects.filter(user=user).count(), 1)

        # The installed Android client can send its next sequence without registering again.
        body.update(sequence=8, idempotency_key=str(uuid4()))
        body["days"][0]["local_date"] = "2026-10-01"
        result = api.post("/api/v1/fitness/sync/", body, format="json")
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result.data["last_sequence"], 8)
        self.assertEqual(DailyActivity.objects.get(user=user, local_date="2026-10-01").steps, 8000)
        refreshed = api.post("/api/v1/auth/refresh/", {"refresh": refresh_token}, format="json")
        self.assertEqual(refreshed.status_code, 200, refreshed.data)
