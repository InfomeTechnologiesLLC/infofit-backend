from copy import deepcopy
from datetime import date
from io import StringIO
from unittest.mock import patch
from uuid import uuid4

from django.core.management import call_command
from django.urls import reverse
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import AccessToken

from accounts.models import User
from challenges.models import Challenge, ChallengeEnrollment, ChallengeProgress, PointsLedger
from challenges.rankings import _steps
from fitness.apple_health import issue_token, token_digest
from fitness.models import (AppleHealthConnection, AppleHealthRecord, AppleHealthSyncBatch,
                            DailyActivity, ExerciseRecord, FitnessAccount, FitnessSource, SleepRecord)


class AppleHealthTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="iphone@example.test", password="long-password")
        self.other = User.objects.create_user(email="other@example.test", password="long-password")
        self.connection, self.token = issue_token(self.user)
        self.source = self.connection.source
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + self.token)
        self.url = reverse("apple_health_sync")

    def metric(self, id="steps-1", type="step_count", value=1200, unit="count", **extra):
        return {"id": "healthkit:" + id, "type": type, "value": value, "unit": unit,
                "start_at": "2026-09-30T08:00:00Z", "end_at": "2026-09-30T09:00:00Z",
                "source_name": "Watch", "source_bundle_id": "com.example.watch", "metadata": {}, **extra}

    def workout(self, **extra):
        return {"id": "healthkit:workout-1", "activity_type": "running",
                "start_at": "2026-09-30T08:00:00Z", "end_at": "2026-09-30T09:00:00Z",
                "duration_seconds": 1800, "source_name": "Watch", "metadata": {}, **extra}

    def body(self, metrics=None, workouts=None, deletions=None, **extra):
        return {"schema_version": 2, "device_id": "iphone-1", "export_id": str(uuid4()),
                "generated_at": "2026-10-01T12:00:00Z", "timezone": "Asia/Kolkata", "source": "ios-healthkit",
                "date_range": {"start": "2026-10-01T12:00:00Z", "end": "2026-10-01T12:00:00Z"},
                "metrics": metrics or [], "workouts": workouts or [], "deletions": deletions or [], **extra}

    def post(self, body):
        response = self.client.post(self.url, body, format="json", HTTP_X_DEVICE_ID=body["device_id"])
        self.assertEqual(response.status_code, 200, response.data)
        return response

    def aggregate(self, value=8000, **extra):
        return self.metric("aggregate:step_count:2026-09-30", value=value,
                           start_at="2026-09-30T00:00:00Z", end_at="2026-10-01T00:00:00Z",
                           source_name="Apple Health", source_bundle_id=None,
                           metadata={"aggregation": "daily_sum", "aggregation_period": "day"}, **extra)

    def enroll(self, kind="steps", target=1000, **extra):
        challenge = Challenge.objects.create(slug="apple-" + str(uuid4()), title="Apple challenge", kind=kind,
            target_steps=target, start_date=date(2026, 9, 30), end_date=date(2026, 9, 30), reward_points=25, **extra)
        return ChallengeEnrollment.objects.create(user=self.user, challenge=challenge, joined_local_date=date(2026, 9, 30))

    def test_actual_client_contract_and_shared_reads(self):
        response = self.post(self.body(metrics=[self.metric()]))
        self.assertEqual({key: response.data[key] for key in ("ok", "received", "duplicates", "deleted")},
                         {"ok": True, "received": 1, "duplicates": 0, "deleted": 0})
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.device_id, "iphone-1")
        self.assertEqual(self.connection.token_hash, token_digest(self.token))
        self.assertNotIn(self.token, self.connection.token_hash)
        day = DailyActivity.objects.get()
        self.assertEqual(day.steps, 1200)
        self.assertEqual(day.verified_value("steps"), 1200)
        self.assertIsNone(day.active_kcal)
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + str(AccessToken.for_user(self.user)))
        response = self.client.get(reverse("fitness_home"), {"date": "2026-09-30"})
        self.assertEqual(response.data["source"], "apple_health")
        self.assertEqual(response.data["metrics"]["steps"], 1200)
        self.assertEqual(response.data["verified_metrics"]["steps"], 1200)

    @patch.dict("sys.modules", {"firebase_admin": None, "redis": None, "urllib3": None, "cryptography": None})
    def test_onboarding_sync_and_browser_reads_without_optional_services(self):
        # The existing server has none of these optional SDKs. iPhone uploads
        # and the employee web app must keep using the existing Django stack.
        _, token = issue_token(self.other)
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + token)
        self.post(self.body(metrics=[self.metric()], workouts=[self.workout()]))
        self.assertEqual(DailyActivity.objects.get(user=self.other).steps, 1200)

        self.client.credentials()
        response = self.client.post("/app/login/", {"token": token})
        self.assertEqual(response.status_code, 302)
        home = self.client.get("/app/api/home/", {"date": "2026-09-30"})
        self.assertEqual(home.status_code, 200)
        self.assertEqual(home.data["metrics"]["steps"], 1200)
        self.assertEqual(home.data["source"], "apple_health")

    def test_normalization_units_workouts_and_sleep(self):
        metrics = [self.metric(), self.metric("distance", "distance_walking_running", 1.5, "km"),
                   self.metric("energy", "active_energy", 418.4, "kJ"),
                   self.metric("exercise", "apple_exercise_time", 1800, "seconds"),
                   self.metric("sleep", "sleep_analysis", 7200, "seconds", metadata={"sleep_stage": "asleep_core"},
                               start_at="2026-09-29T22:00:00Z", end_at="2026-09-30T00:00:00Z"),
                   self.metric("bed", "sleep_analysis", 14400, "seconds", metadata={"sleep_stage": "in_bed"}),
                   self.metric("awake", "sleep_analysis", 3600, "seconds", metadata={"sleep_stage": "awake"})]
        self.post(self.body(metrics=metrics, workouts=[self.workout(activity_type="traditionalStrengthTraining")]))
        day = DailyActivity.objects.get(local_date="2026-09-30")
        self.assertEqual(day.distance_m, 1500)
        self.assertAlmostEqual(day.active_kcal, 100)
        self.assertEqual(day.exercise_minutes, 30)
        self.assertEqual(day.sleep_minutes, 120)
        self.assertEqual(SleepRecord.objects.count(), 1)
        workout = ExerciseRecord.objects.get()
        self.assertEqual(workout.exercise_type, "strength_training")
        self.assertEqual(workout.duration_seconds, 1800)
        self.assertEqual((workout.end_at - workout.start_at).total_seconds(), 3600)

    def test_retry_changed_export_and_atomic_failure(self):
        body = self.body(metrics=[self.metric()])
        result = self.post(body).data
        self.assertEqual(self.post(body).data, result)
        self.assertEqual(AppleHealthRecord.objects.count(), 1)
        self.assertEqual(AppleHealthSyncBatch.objects.count(), 1)
        body["metrics"][0]["value"] = 2000
        self.assertEqual(self.client.post(self.url, body, format="json").status_code, 409)
        self.assertEqual(DailyActivity.objects.get().steps, 1200)
        with patch("fitness.apple_health.reconcile_user", side_effect=RuntimeError("rollback")), self.assertLogs("django.request", "ERROR"):
            failed = self.client.post(self.url, self.body(metrics=[self.metric("second")]), format="json")
            self.assertEqual(failed.status_code, 500)
        self.assertEqual(AppleHealthRecord.objects.count(), 1)
        self.assertEqual(AppleHealthSyncBatch.objects.count(), 1)

    def test_updates_older_arrivals_and_date_changes(self):
        self.post(self.body(metrics=[self.metric()]))
        corrected = self.metric(value=600, start_at="2026-09-29T08:00:00Z", end_at="2026-09-29T09:00:00Z")
        self.post(self.body(metrics=[corrected], generated_at="2026-10-02T12:00:00Z"))
        self.assertIsNone(DailyActivity.objects.get(local_date="2026-09-30").steps)
        self.assertEqual(DailyActivity.objects.get(local_date="2026-09-29").steps, 600)
        result = self.post(self.body(metrics=[self.metric()], generated_at="2026-10-01T13:00:00Z"))
        self.assertEqual(result.data["duplicates"], 1)
        self.assertEqual(DailyActivity.objects.get(local_date="2026-09-29").steps, 600)

    def test_deletions_tombstones_and_delayed_reexport(self):
        self.post(self.body(metrics=[self.metric()], workouts=[self.workout()]))
        deletes = [{"kind": "metric", "id": "healthkit:steps-1"}, {"kind": "workout", "id": "healthkit:workout-1"},
                   {"kind": "metric", "id": "healthkit:unseen"}]
        result = self.post(self.body(deletions=deletes, generated_at="2026-10-02T12:00:00Z"))
        self.assertEqual(result.data["deleted"], 2)
        self.assertIsNone(DailyActivity.objects.get().steps)
        self.assertEqual(ExerciseRecord.objects.count(), 0)
        self.post(self.body(metrics=[self.metric(), self.metric("unseen")], generated_at="2026-10-03T12:00:00Z"))
        self.assertEqual(AppleHealthRecord.objects.filter(deleted=True).count(), 3)
        self.assertIsNone(DailyActivity.objects.get().steps)

    def test_summaries_count_for_rankings_and_rewards_without_double_counting_samples(self):
        enrollment = self.enroll()
        self.post(self.body(metrics=[self.aggregate()], timezone="UTC"))
        day = DailyActivity.objects.get()
        self.assertEqual(day.steps, 8000)
        self.assertEqual(day.verified_value("steps"), 8000)
        self.assertEqual(list(PointsLedger.objects.values_list("delta", flat=True)), [25])
        self.assertEqual(_steps([self.user.pk], day.local_date, day.local_date), {self.user.pk: 8000})
        self.post(self.body(metrics=[self.metric()], timezone="UTC", generated_at="2026-10-02T12:00:00Z"))
        day.refresh_from_db()
        self.assertEqual(day.steps, 8000)
        self.assertEqual(day.verified_value("steps"), 8000)
        self.assertTrue(ChallengeProgress.objects.get(enrollment=enrollment).is_complete)
        self.assertEqual(_steps([self.user.pk], day.local_date, day.local_date), {self.user.pk: 8000})
        self.assertEqual(PointsLedger.objects.count(), 1)

    def test_summaries_count_for_distance_calories_and_step_day_challenges(self):
        distance = self.enroll(kind="distance", target_value=5)
        calories = self.enroll(kind="calories", target_value=300)
        step_days = self.enroll(kind="step_days", daily_threshold=7000, target_value=1)
        self.post(self.body(metrics=[self.aggregate(),
            {**self.aggregate(value=6), "id": "healthkit:aggregate:distance", "type": "distance_walking_running", "unit": "km"},
            {**self.aggregate(value=400), "id": "healthkit:aggregate:energy", "type": "active_energy", "unit": "kcal"}], timezone="UTC"))
        day = DailyActivity.objects.get()
        self.assertEqual(day.verified_metrics, {"steps": 8000, "distance_m": 6000, "active_kcal": 400})
        for enrollment, value in ((distance, 6), (calories, 400), (step_days, 1)):
            progress = ChallengeProgress.objects.get(enrollment=enrollment)
            self.assertTrue(progress.is_complete)
            self.assertEqual(progress.progress_value, value)

    def test_summary_credit_excludes_known_manual_steps_inside_and_outside_summary(self):
        summary = self.aggregate()
        summary["end_at"] = "2026-09-30T12:00:00Z"
        inside = self.metric("manual-inside", value=2000, metadata={"HKWasUserEntered": "true"})
        outside = self.metric("manual-outside", value=500, metadata={"HKWasUserEntered": "true"},
                              start_at="2026-09-30T15:00:00Z", end_at="2026-09-30T16:00:00Z")
        self.post(self.body(metrics=[summary, self.metric(), inside, outside], timezone="UTC"))
        day = DailyActivity.objects.get()
        self.assertEqual(day.steps, 8500)
        self.assertEqual(day.verified_value("steps"), 6000)
        self.assertEqual(_steps([self.user.pk], day.local_date, day.local_date), {self.user.pk: 6000})

    def test_summary_correction_reverses_reward_and_zero_is_still_data(self):
        enrollment = self.enroll()
        self.post(self.body(metrics=[self.aggregate()], timezone="UTC"))
        self.post(self.body(metrics=[self.aggregate(value=0)], timezone="UTC", generated_at="2026-10-02T12:00:00Z"))
        day = DailyActivity.objects.get()
        self.assertEqual(day.verified_value("steps"), 0)
        progress = ChallengeProgress.objects.get(enrollment=enrollment)
        self.assertTrue(progress.has_step_data)
        self.assertFalse(progress.is_complete)
        self.assertEqual(list(PointsLedger.objects.order_by("revision").values_list("delta", flat=True)), [25, -25])

    def test_rebuild_restores_legacy_summary_credit_and_awards_only_once(self):
        self.post(self.body(metrics=[self.aggregate(), self.metric()], timezone="UTC"))
        enrollment = self.enroll(target=5000)
        day = DailyActivity.objects.get()
        source_sync = FitnessSource.objects.get(pk=self.source.pk).last_successful_sync_at
        # Previously, summary-only days had null credit and mixed days only counted raw samples.
        for old_credit in (None, 1200):
            DailyActivity.objects.filter(pk=day.pk).update(verified_metrics={"steps": old_credit})
            call_command("rebuild_apple_health_activity", self.user.email, stdout=StringIO())
            day.refresh_from_db()
            self.assertEqual(day.steps, 8000)
            self.assertEqual(day.verified_value("steps"), 8000)
            self.assertTrue(ChallengeProgress.objects.get(enrollment=enrollment).is_complete)
            self.assertEqual(_steps([self.user.pk], day.local_date, day.local_date), {self.user.pk: 8000})
        self.assertEqual(list(PointsLedger.objects.values_list("delta", flat=True)), [25])
        self.assertEqual(AppleHealthRecord.objects.count(), 2)
        self.assertEqual(AppleHealthSyncBatch.objects.count(), 1)
        self.assertEqual(FitnessSource.objects.get(pk=self.source.pk).last_successful_sync_at, source_sync)

    def test_rebuild_skips_history_of_inactive_apple_source(self):
        self.post(self.body(metrics=[self.aggregate()], timezone="UTC"))
        android = FitnessSource.objects.create(user=self.user, installation_id=uuid4())
        FitnessAccount.objects.filter(user=self.user).update(active_source=android)
        DailyActivity.objects.filter(user=self.user).update(source=android, steps=600, verified_metrics={})
        call_command("rebuild_apple_health_activity", stdout=StringIO())
        day = DailyActivity.objects.get()
        self.assertEqual((day.source_id, day.steps, day.verified_value("steps")), (android.pk, 600, 600))

    def test_partial_summary_and_uncovered_samples(self):
        aggregate = self.aggregate()
        aggregate["end_at"] = "2026-09-30T12:00:00Z"
        later = self.metric("later", value=500, start_at="2026-09-30T15:00:00Z", end_at="2026-09-30T16:00:00Z")
        self.post(self.body(metrics=[aggregate, self.metric(), later], timezone="UTC"))
        day = DailyActivity.objects.get()
        self.assertEqual(day.steps, 8500)
        self.assertEqual(day.verified_value("steps"), 8500)

    def test_summary_corrections_and_raw_deletion_invalidation(self):
        self.post(self.body(metrics=[self.aggregate(), self.metric()], timezone="UTC"))
        self.post(self.body(metrics=[self.aggregate(value=6000)], timezone="UTC", generated_at="2026-10-02T12:00:00Z"))
        self.assertEqual(DailyActivity.objects.get().steps, 6000)
        self.post(self.body(metrics=[self.aggregate(value=9000)], timezone="UTC", generated_at="2026-10-01T13:00:00Z"))
        self.assertEqual(DailyActivity.objects.get().steps, 6000)
        self.post(self.body(deletions=[{"kind": "metric", "id": "healthkit:steps-1"}], timezone="UTC",
                            generated_at="2026-10-03T12:00:00Z"))
        self.assertIsNone(DailyActivity.objects.get().steps)
        # A fresh authoritative re-export can restore a corrected daily summary.
        self.post(self.body(metrics=[self.aggregate(value=6000)], timezone="UTC", generated_at="2026-10-04T12:00:00Z"))
        self.assertEqual(DailyActivity.objects.get().steps, 6000)

    def test_phone_watch_overlap_and_manual_entries(self):
        self.enroll(target=1500)
        phone = self.metric("phone", value=1000, source_bundle_id="com.example.phone")
        manual = self.metric("manual", value=9000, metadata={"HKWasUserEntered": "1"})
        self.post(self.body(metrics=[self.metric(), phone, manual]))
        day = DailyActivity.objects.get()
        self.assertEqual(day.steps, 10200)
        self.assertEqual(day.manual_steps, 9000)
        self.assertEqual(day.verified_value("steps"), 1200)
        self.assertEqual(PointsLedger.objects.count(), 0)

    def test_challenge_award_reversal_after_deletion(self):
        self.enroll()
        self.post(self.body(metrics=[self.metric()]))
        self.assertEqual(list(PointsLedger.objects.values_list("delta", flat=True)), [25])
        self.post(self.body(deletions=[{"kind": "metric", "id": "healthkit:steps-1"}], generated_at="2026-10-02T12:00:00Z"))
        self.assertEqual(list(PointsLedger.objects.values_list("delta", flat=True)), [25, -25])

    def test_paused_workouts_and_manual_sessions(self):
        enrollment = self.enroll(kind="running", target_running_minutes=40)
        self.post(self.body(workouts=[self.workout(), self.workout(id="healthkit:manual", metadata={"HKWasUserEntered": "true"})]))
        progress = ChallengeProgress.objects.get(enrollment=enrollment)
        self.assertEqual(progress.progress_value, 30)
        self.assertFalse(progress.is_complete)

    def test_sleep_stages_overlap_midnight_and_deletion(self):
        first = self.metric("sleep-1", "sleep_analysis", 7200, "seconds", metadata={"sleep_stage": "asleep_core"},
                            start_at="2026-09-29T22:00:00Z", end_at="2026-09-30T00:00:00Z")
        second = self.metric("sleep-2", "sleep_analysis", 7200, "seconds", metadata={"sleep_stage": "asleep_deep"},
                             start_at="2026-09-30T00:00:00Z", end_at="2026-09-30T02:00:00Z")
        duplicate = {**second, "id": "healthkit:other-sleep", "source_name": "Other watch"}
        self.post(self.body(metrics=[first, second, duplicate], timezone="UTC"))
        day = DailyActivity.objects.get(local_date="2026-09-30")
        self.assertEqual(day.sleep_minutes, 240)
        self.assertEqual(set(SleepRecord.objects.values_list("local_date", flat=True)), {date(2026, 9, 30)})
        self.post(self.body(deletions=[{"kind": "metric", "id": first["id"]}], timezone="UTC", generated_at="2026-10-02T12:00:00Z"))
        day.refresh_from_db()
        self.assertEqual(day.sleep_minutes, 120)

    def test_midnight_split_and_dst_daily_summary(self):
        crossing = self.metric(value=100, start_at="2026-09-30T18:00:00Z", end_at="2026-09-30T19:00:00Z")
        self.post(self.body(metrics=[crossing]))
        self.assertEqual(list(DailyActivity.objects.order_by("local_date").values_list("steps", flat=True)), [50, 50])
        dst = self.aggregate(value=2300)
        dst.update(id="healthkit:aggregate:step_count:2026-03-08", start_at="2026-03-08T05:00:00Z", end_at="2026-03-09T04:00:00Z")
        self.post(self.body(metrics=[dst], timezone="America/New_York"))
        self.assertEqual(DailyActivity.objects.get(local_date="2026-03-08").steps, 2300)
        self.assertFalse(DailyActivity.objects.filter(local_date="2026-03-09").exists())

    def test_unknown_metrics_are_retained_without_fabricating_activity(self):
        self.post(self.body(metrics=[self.metric("hrv", "hrv_sdnn", 43.5, "ms")]))
        self.assertEqual(AppleHealthRecord.objects.get().payload["value"], 43.5)
        self.assertEqual(DailyActivity.objects.count(), 0)

    def test_original_samples_work_as_separate_chunks_with_same_generation_time(self):
        self.post(self.body(metrics=[self.metric("first", value=500)]))
        self.post(self.body(metrics=[self.metric("second", value=300)]))
        self.assertEqual(DailyActivity.objects.get().steps, 800)
        duplicate = self.post(self.body(metrics=[self.metric("first", value=500)]))
        self.assertEqual(duplicate.data["duplicates"], 1)
        self.assertEqual(DailyActivity.objects.get().steps, 800)

    def test_instantaneous_samples_and_partial_phone_watch_overlap(self):
        watch = self.metric(value=1000, start_at="2026-09-30T08:00:00Z", end_at="2026-09-30T10:00:00Z")
        phone = self.metric("phone", value=800, source_bundle_id="phone",
                            start_at="2026-09-30T09:00:00Z", end_at="2026-09-30T11:00:00Z")
        instant = self.metric("instant", value=100, start_at="2026-09-30T12:00:00Z", end_at="2026-09-30T12:00:00Z")
        self.post(self.body(metrics=[watch, phone, instant], timezone="UTC"))
        self.assertEqual(DailyActivity.objects.get().steps, 1500)

    def test_later_sleep_stages_move_evening_sleep_to_waking_day(self):
        evening = self.metric("evening", "sleep_analysis", 5400, "seconds", metadata={"sleep_stage": "asleep_core"},
                              start_at="2026-09-29T22:00:00Z", end_at="2026-09-29T23:30:00Z")
        morning = self.metric("morning", "sleep_analysis", 3600, "seconds", metadata={"sleep_stage": "asleep_rem"},
                              start_at="2026-09-30T00:00:00Z", end_at="2026-09-30T01:00:00Z")
        self.post(self.body(metrics=[evening], timezone="UTC"))
        self.assertEqual(DailyActivity.objects.get(local_date="2026-09-29").sleep_minutes, 90)
        self.post(self.body(metrics=[morning], timezone="UTC", generated_at="2026-10-02T12:00:00Z"))
        self.assertIsNone(DailyActivity.objects.get(local_date="2026-09-29").sleep_minutes)
        self.assertEqual(DailyActivity.objects.get(local_date="2026-09-30").sleep_minutes, 150)
        self.assertEqual(set(SleepRecord.objects.values_list("local_date", flat=True)), {date(2026, 9, 30)})

    def test_a_batch_cannot_reuse_an_export_id_for_another_device(self):
        body = self.body(metrics=[self.metric()])
        self.post(body)
        body["device_id"] = "another-phone"
        self.assertEqual(self.client.post(self.url, body, format="json").status_code, 409)

    def test_android_upload_cannot_overwrite_apple_projections(self):
        self.post(self.body(metrics=[self.metric()]))
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + str(AccessToken.for_user(self.user)))
        body = {"installation_id": str(self.source.installation_id), "idempotency_key": str(uuid4()),
                "sequence": 1, "time_zone": "UTC", "days": [{"local_date": "2026-09-30",
                "observed_at": "2026-10-01T12:00:00Z", "steps": 99999,
                "metric_status": {"steps": "available", "distance_m": "no_data", "active_kcal": "no_data",
                                  "exercise_minutes": "no_data", "sleep_minutes": "no_data"}, "exercises": [], "sleep": []}]}
        self.assertEqual(self.client.post(reverse("fitness_sync"), body, format="json").status_code, 409)
        self.assertEqual(DailyActivity.objects.get().steps, 1200)

    def test_missing_categories_are_not_zero_or_deleted(self):
        self.post(self.body(metrics=[self.metric()]))
        self.post(self.body(metrics=[self.metric("energy", "active_energy", 100, "kcal")]))
        day = DailyActivity.objects.get()
        self.assertEqual(day.steps, 1200)
        self.assertEqual(day.active_kcal, 100)
        self.assertIsNone(day.sleep_minutes)
        self.post(self.body())
        self.assertEqual(DailyActivity.objects.get().steps, 1200)

    def test_authentication_scopes_device_binding_and_isolation(self):
        self.post(self.body(metrics=[self.metric()]))
        response = self.client.post(self.url, self.body(device_id="another-phone"), format="json")
        self.assertEqual(response.status_code, 409)
        response = self.client.post(self.url, self.body(), format="json", HTTP_X_DEVICE_ID="wrong")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get(reverse("fitness_home")).status_code, 401)
        self.assertEqual(self.client.post(reverse("apple_health_token"), {}, format="json").status_code, 401)
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + str(AccessToken.for_user(self.user)))
        self.assertEqual(self.client.post(self.url, self.body(), format="json").status_code, 401)
        _, other_token = issue_token(self.other)
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + other_token)
        self.post(self.body(deletions=[{"kind": "metric", "id": "healthkit:steps-1"}]))
        self.assertEqual(DailyActivity.objects.get(user=self.user).steps, 1200)
        self.assertFalse(AppleHealthRecord.objects.get(source=self.source).deleted)
        self.user.is_active = False
        self.user.save()
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + self.token)
        self.assertEqual(self.client.post(self.url, self.body(), format="json").status_code, 401)

    def test_token_api_rotation_revocation_and_new_phone(self):
        self.post(self.body(metrics=[self.metric()]))
        jwt = str(AccessToken.for_user(self.user))
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + jwt)
        info = self.client.get(reverse("apple_health_token"))
        self.assertEqual(info.data["device_id"], "iphone-1")
        self.assertNotIn("token", info.data)
        rotated = self.client.post(reverse("apple_health_token"), {}, format="json")
        self.assertEqual(rotated.status_code, 201)
        self.assertEqual(rotated["Cache-Control"], "no-store")
        self.assertEqual(rotated.data["installation_id"], str(self.source.installation_id))
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + self.token)
        self.assertEqual(self.client.post(self.url, self.body(), format="json").status_code, 401)
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + rotated.data["token"])
        self.post(self.body())
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + jwt)
        response = self.client.post(reverse("apple_health_token"), {"new_device": True}, format="json")
        self.assertEqual(response.status_code, 409)
        response = self.client.post(reverse("apple_health_token"), {"new_device": True, "replace_existing": True}, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertNotEqual(response.data["installation_id"], str(self.source.installation_id))
        new_token = response.data["token"]
        self.assertEqual(self.client.delete(reverse("apple_health_token")).status_code, 204)
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + new_token)
        self.assertEqual(self.client.post(self.url, self.body(), format="json").status_code, 401)

    def test_source_switch_is_explicit_and_android_cannot_write_apple_source(self):
        self.post(self.body(metrics=[self.metric()]))
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + str(AccessToken.for_user(self.user)))
        android = str(uuid4())
        payload = {"installation_id": android, "time_zone": "UTC", "replace_existing": True}
        self.assertEqual(self.client.put(reverse("fitness_source"), payload, format="json").status_code, 200)
        self.assertEqual(self.client.post(reverse("apple_health_token"), {}, format="json").status_code, 409)
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + self.token)
        self.assertEqual(self.client.post(self.url, self.body(), format="json").status_code, 409)
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + str(AccessToken.for_user(self.user)))
        response = self.client.post(reverse("apple_health_token"), {"replace_existing": True}, format="json")
        self.assertEqual(response.status_code, 201)
        account = FitnessAccount.objects.get(user=self.user)
        self.assertEqual(account.active_source_id, self.source.pk)
        payload["installation_id"] = str(self.source.installation_id)
        self.assertEqual(self.client.put(reverse("fitness_source"), payload, format="json").status_code, 409)

    def test_malformed_batches_are_rejected_atomically(self):
        valid = self.body(metrics=[self.metric()])
        invalid = []
        for field, value in (("schema_version", 99), ("timezone", "Mars/Olympus"), ("generated_at", "2026-10-01T12:00:00")):
            body = deepcopy(valid)
            body[field] = value
            invalid.append(body)
        for field, value in (("value", -1), ("unit", "bananas"), ("end_at", "2026-09-29T09:00:00Z"), ("value", "NaN")):
            body = deepcopy(valid)
            body["metrics"][0][field] = value
            invalid.append(body)
        body = deepcopy(valid)
        body["deletions"] = [{"kind": "metric", "id": "healthkit:steps-1"}]
        invalid.append(body)
        body = deepcopy(valid)
        body["metrics"].append(self.metric())
        invalid.append(body)
        for body in invalid:
            with self.subTest(body=body):
                self.assertEqual(self.client.post(self.url, body, format="json").status_code, 400)
        self.assertEqual(AppleHealthRecord.objects.count(), 0)
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.device_id, "")

    def test_management_command_and_account_cleanup(self):
        output = StringIO()
        call_command("issue_apple_health_token", self.other.email, stdout=output)
        token = output.getvalue().strip()
        self.assertTrue(token.startswith("ahs_"))
        self.assertEqual(AppleHealthConnection.objects.get(user=self.other).token_hash, token_digest(token))
        self.post(self.body(metrics=[self.metric()]))
        self.user.delete()
        self.assertFalse(AppleHealthRecord.objects.filter(source_id=self.source.pk).exists())
        self.assertFalse(AppleHealthSyncBatch.objects.filter(source_id=self.source.pk).exists())
        self.assertFalse(DailyActivity.objects.exists())

    def test_large_normalized_total_rolls_back_and_unknown_workout_names_are_safe(self):
        body = self.body(metrics=[self.metric(value=1_000_001)])
        self.assertEqual(self.client.post(self.url, body, format="json").status_code, 400)
        self.assertFalse(AppleHealthRecord.objects.exists())
        self.assertFalse(AppleHealthSyncBatch.objects.exists())
        self.post(self.body(workouts=[self.workout(activity_type="step_count")]))
        self.assertIsNone(DailyActivity.objects.get().steps)
        self.assertEqual(ExerciseRecord.objects.get().exercise_type, "step_count")
