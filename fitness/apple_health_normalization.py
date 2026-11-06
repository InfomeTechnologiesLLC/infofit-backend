"""Project HealthySync records into the shared fitness tables.

HealthKit's source-priority list is not exported. For overlapping raw quantities we use
the largest per-source rate, rather than summing phone and watch readings. Daily HealthKit
summaries take precedence within their interval for both display and competition credit.
Explicitly manual readings are subtracted from credit; a summary's unknown manual-entry
breakdown does not prevent the rest of that day's activity from counting.
"""
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone as dt_timezone
import re

from rest_framework.exceptions import ValidationError

from .models import AppleHealthRecord, DailyActivity, ExerciseRecord, SleepRecord
from .serializers import METRICS, ZoneInfo

QUANTITIES = {
    "step_count": ("steps", {"count": 1}),
    "distance_walking_running": ("distance_m", {"m": 1, "km": 1000, "cm": .01, "mi": 1609.344}),
    "active_energy": ("active_kcal", {"kcal": 1, "kJ": 1 / 4.184, "J": 1 / 4184}),
    "apple_exercise_time": ("exercise_minutes", {"minutes": 1, "min": 1, "seconds": 1 / 60, "s": 1 / 60}),
}
ASLEEP = {"asleep", "asleep_unspecified", "asleep_core", "asleep_deep", "asleep_rem"}
WORKOUT_TYPES = {
    "cycling": "biking", "swimming": "swimming_pool", "dance": "dancing",
    "dance_inspired_training": "dancing", "cardio_dance": "dancing", "social_dance": "dancing",
    "functional_strength_training": "strength_training", "traditional_strength_training": "strength_training",
    "core_training": "calisthenics", "american_football": "football_american",
    "australian_football": "football_australian", "flexibility": "stretching",
    "stairs": "stair_climbing", "rowing": "rowing_machine", "kickboxing": "martial_arts",
}


def workout_type(value):
    name = re.sub(r"(?<!^)(?=[A-Z])", "_", value).lower()
    # Do not interpret an unknown HealthKit numeric enum as a Health Connect enum.
    if name.isdigit():
        return "hk_" + name
    return WORKOUT_TYPES.get(name, name)


def is_manual(record):
    value = record.payload.get("metadata", {}).get("HKWasUserEntered", "")
    return str(value).strip().lower() in ("1", "true", "yes")


def is_aggregate(record):
    return record.payload.get("metadata", {}).get("aggregation") in ("daily_sum", "daily_average")


def date_bounds(kind, record_type, start, end, zone_name):
    zone = ZoneInfo(zone_name)
    if kind == "workout":
        day = end.astimezone(zone).date()
        return day, day
    first = start.astimezone(zone).date()
    last = (end - timedelta(microseconds=1) if end > start else end).astimezone(zone).date()
    if record_type == "sleep_analysis":
        last = end.astimezone(zone).date()
    return first, last


def affected_dates(record):
    if record.first_date is None:
        return set()
    # A night can cross midnight; a later stage can move all its earlier stages to the waking date.
    padding = 1 if record.record_type == "sleep_analysis" else 0
    first = record.first_date - timedelta(days=padding)
    last = record.last_date + timedelta(days=padding)
    return {first + timedelta(days=n) for n in range((last - first).days + 1)}


def day_segment(record, day):
    zone = ZoneInfo(record.time_zone)
    start = datetime.combine(day, time.min, tzinfo=zone).astimezone(dt_timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone).astimezone(dt_timezone.utc)
    if record.start_at == record.end_at:
        return (record.start_at, record.end_at) if start <= record.start_at < end else None
    left, right = max(start, record.start_at), min(end, record.end_at)
    return (left, right) if right > left else None


def quantity_total(records, day, summaries=True, automatic_only=False):
    """Sweep intervals once. Different sources overlap, records from the same source accumulate."""
    events = defaultdict(list)
    points = defaultdict(list)
    for record in records:
        aggregate = is_aggregate(record)
        if (aggregate and not summaries) or (automatic_only and is_manual(record)):
            continue
        segment = day_segment(record, day)
        if segment is None:
            continue
        start, end = segment
        value = record.payload["value"] * QUANTITIES[record.record_type][1][record.payload["unit"]]
        group = record.payload.get("source_bundle_id") or record.payload.get("source_name") or "unknown"
        mode = "aggregate" if aggregate else "manual" if is_manual(record) else "raw"
        seconds = (record.end_at - record.start_at).total_seconds()
        if seconds == 0:
            points[start].append((mode, group, value))
            events[start]  # Ensure point samples are visited by the sweep.
        else:
            entry = (mode, group, value / seconds, record.generated_at, record.record_id)
            events[start].append((True, entry))
            events[end].append((False, entry))
    active = {}
    total = 0.0
    previous = None
    for instant in sorted(events):
        if previous is not None:
            aggregates = [entry for entry in active.values() if entry[0] == "aggregate"]
            if aggregates:
                rate = max(aggregates, key=lambda entry: (entry[3], entry[4]))[2]
            else:
                sources = defaultdict(float)
                manual = 0.0
                for mode, group, rate, _, _ in active.values():
                    if mode == "manual":
                        manual += rate
                    else:
                        sources[group] += rate
                rate = max(sources.values(), default=0.0) + manual
            total += rate * (instant - previous).total_seconds()
        for adding, entry in events[instant]:
            if adding:
                active[entry[4]] = entry
            else:
                active.pop(entry[4], None)
        if not any(entry[0] == "aggregate" for entry in active.values()):
            sources = defaultdict(float)
            manual = 0.0
            for mode, group, value in points[instant]:
                if mode == "manual":
                    manual += value
                else:
                    sources[group] += value
            total += max(sources.values(), default=0.0) + manual
        previous = instant
    return max(0.0, total)


def merged_seconds(intervals):
    total = 0.0
    right = None
    for start, end in sorted(intervals):
        if right is None or start >= right:
            total += max(0, (end - start).total_seconds())
        elif end > right:
            total += (end - right).total_seconds()
        right = max(right, end) if right is not None else end
    return total


def sleep_nights(records):
    """Assign nearby asleep stages to their waking date, preserving gaps and stage timestamps."""
    records = sorted((record for record in records if not record.deleted and record.kind == "metric" and record.record_type == "sleep_analysis"
                      and record.payload.get("metadata", {}).get("sleep_stage") in ASLEEP
                      and record.end_at > record.start_at), key=lambda record: record.start_at)
    nights = defaultdict(list)
    group = []
    end = None

    def finish():
        if group:
            last = max(group, key=lambda record: record.end_at)
            nights[last.end_at.astimezone(ZoneInfo(last.time_zone)).date()].extend(group)

    for record in records:
        if group and (record.start_at > end + timedelta(hours=2)
                      or record.end_at - group[0].start_at > timedelta(hours=24)):
            finish()
            group = []
            end = None
        group.append(record)
        end = max(end, record.end_at) if end is not None else record.end_at
    finish()
    return nights


def normalize_days(source, dates, observed_at, time_zone):
    if not dates:
        return
    # Query only the affected windows. Neighbouring sleep stages establish the waking date.
    from django.db.models import Q
    query = Q(pk__in=[])
    windows = []
    for day in sorted(dates):
        left, right = day - timedelta(days=1), day + timedelta(days=1)
        if windows and left <= windows[-1][1] + timedelta(days=1):
            windows[-1] = (windows[-1][0], right)
        else:
            windows.append((left, right))
    for left, right in windows:
        query |= Q(first_date__lte=right, last_date__gte=left)
    supported = Q(kind="workout") | Q(record_type__in=[*QUANTITIES, "sleep_analysis"])
    records = list(AppleHealthRecord.objects.filter(query, supported, source=source))
    nights = sleep_nights(records)
    for day in sorted(dates):
        values = {metric: None for metric in METRICS}
        verified = {metric: None for metric in ("steps", "distance_m", "active_kcal")}
        manual = {"manual_" + metric: None for metric in verified}
        current = [record for record in records if record.first_date <= day <= record.last_date]
        for record_type, (metric, _) in QUANTITIES.items():
            samples = [record for record in current if record.kind == "metric" and record.record_type == record_type and not record.deleted]
            tombstones = [record for record in current if record.kind == "metric" and record.record_type == record_type and record.deleted]
            # A deletion makes an older summary unreliable; keep its raw record for audit, but reproject samples.
            samples = [record for record in samples if not is_aggregate(record) or not any(
                deleted.generated_at >= record.generated_at and deleted.start_at < record.end_at
                and deleted.end_at >= record.start_at for deleted in tombstones)]
            if not samples:
                continue
            amount = quantity_total(samples, day)
            values[metric] = int(round(amount)) if metric in ("steps", "exercise_minutes") else round(amount, 6)
            if metric == "exercise_minutes":
                values[metric] = min(1440, values[metric])
            if metric in verified:
                # Use the same summary-first daily total as Home. Raw samples inside a
                # summary must neither add to it nor replace it with a partial total.
                known_manual = quantity_total([record for record in samples if is_manual(record)], day)
                credited = max(0.0, amount - known_manual)
                verified[metric] = int(round(credited)) if metric == "steps" else round(credited, 6)
                # Null still means the export has no complete manual-entry breakdown.
                if not any(is_aggregate(record) for record in samples):
                    manual["manual_" + metric] = int(round(known_manual)) if metric == "steps" else round(known_manual, 6)

        workouts = [record for record in current if record.kind == "workout" and not record.deleted]
        keep = []
        spans = []
        for record in workouts:
            keep.append(record.record_id)
            duration = min(record.payload["duration_seconds"], (record.end_at - record.start_at).total_seconds())
            spans.append((record.start_at, record.start_at + timedelta(seconds=duration)))
            ExerciseRecord.objects.update_or_create(user=source.user, source=source, record_id=record.record_id,
                defaults={"origin_package": record.payload.get("source_bundle_id") or record.payload["source_name"],
                          "local_date": day, "start_at": record.start_at, "end_at": record.end_at,
                          "duration_seconds": duration, "exercise_type": workout_type(record.payload["activity_type"]),
                          "recording_method": "manual" if is_manual(record) else "automatic"})
        ExerciseRecord.objects.filter(source=source, local_date=day).exclude(record_id__in=keep).delete()
        if values["exercise_minutes"] is None and workouts:
            values["exercise_minutes"] = min(1440, int(merged_seconds(spans) // 60))

        stages = nights.get(day, [])
        keep = []
        for record in stages:
            keep.append(record.record_id)
            SleepRecord.objects.update_or_create(user=source.user, source=source, record_id=record.record_id,
                defaults={"origin_package": record.payload.get("source_bundle_id") or record.payload["source_name"],
                          "local_date": day, "start_at": record.start_at, "end_at": record.end_at,
                          "recording_method": "manual" if is_manual(record) else "automatic"})
        SleepRecord.objects.filter(source=source, local_date=day).exclude(record_id__in=keep).delete()
        if stages:
            values["sleep_minutes"] = min(1440, int(merged_seconds([(r.start_at, r.end_at) for r in stages]) // 60))

        for metric, maximum in (("steps", 1_000_000), ("distance_m", 1_000_000), ("active_kcal", 100_000)):
            if values[metric] is not None and values[metric] > maximum:
                raise ValidationError({metric: "Normalized daily total exceeds the supported maximum."})

        existing = DailyActivity.objects.filter(user=source.user, local_date=day).first()
        if all(value is None for value in values.values()) and (existing is None or existing.source_id != source.pk):
            continue
        contributing = current + stages
        latest = max(contributing, key=lambda record: record.generated_at) if contributing else None
        DailyActivity.objects.update_or_create(user=source.user, local_date=day,
            defaults={"source": source, "time_zone": latest.time_zone if latest else time_zone,
                      "observed_at": latest.generated_at if latest else observed_at,
                      "metric_status": {key: "available" if value is not None else "no_data" for key, value in values.items()},
                      "verified_metrics": verified, **values, **manual})
