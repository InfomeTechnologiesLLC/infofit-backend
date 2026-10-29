"""How every challenge kind is scored, using only data the phone or watch recorded by itself.

Anti-cheat rules (the same for every kind):
- Only the employee's ONE active phone counts (a second phone cannot add to the total).
- Anything typed in by hand in a fitness app (Health Connect "manual entry") is subtracted or ignored.
- Daily totals only count full days after the joining day (a day's total cannot be split at the joining time);
  workout sessions must start after the moment of joining.
- Overlapping workout sessions (a watch and a phone app recording the same walk) are merged, never double counted.
- Workout sessions shorter than 10 minutes are ignored; one session counts for at most 4 hours.
- Impossible days are capped (e.g. more than 50,000 steps or 60 km) and counted as "flagged days" for staff.
"""
from datetime import timedelta

from fitness.models import DailyActivity, ExerciseRecord, SleepRecord

DAILY_CAPS = {"steps": 50_000, "distance_km": 60.0, "calories": 2_500.0}
MIN_SESSION = timedelta(minutes=10)
MAX_SESSION = timedelta(hours=4)
MAX_SLEEP_NIGHT = timedelta(hours=16)

# Health Connect ExerciseSessionRecord type numbers (older app versions sent names such as "running").
RUNNING_TYPES = ("running", "running_treadmill", "56", "57")
ACTIVITY_TYPES = {
    "cycling": ("8", "9", "biking", "biking_stationary"),
    "swimming": ("73", "74", "swimming_open_water", "swimming_pool"),
    "walking": ("79", "walking"),
    "hiking": ("37", "hiking"),
    "yoga": ("83", "48", "71", "yoga", "pilates", "stretching"),
    "strength": ("70", "81", "13", "strength_training", "weightlifting", "calisthenics"),
    "hiit": ("36", "10", "26", "high_intensity_interval_training", "boot_camp", "exercise_class"),
    "racket": ("2", "50", "66", "75", "76", "badminton", "racquetball", "squash", "table_tennis", "tennis"),
    "team_sports": ("4", "5", "14", "28", "29", "35", "55", "64", "65", "78", "80", "baseball", "basketball",
                    "cricket", "football_american", "football_australian", "handball", "rugby", "soccer",
                    "softball", "volleyball", "water_polo"),
    "dancing": ("16", "dancing"),
    "cardio_machines": ("25", "54", "68", "69", "elliptical", "rowing_machine", "stair_climbing",
                        "stair_climbing_machine"),
    "martial_arts": ("44", "11", "27", "martial_arts", "boxing", "fencing"),
}


class Score:
    """Result of scoring one enrollment (a plain class so it also runs on Python 3.6)."""

    def __init__(self, value=0, has_data=False, flagged_days=0):
        self.value = value
        self.has_data = has_data
        self.flagged_days = flagged_days

    def __eq__(self, other):
        return isinstance(other, Score) and (self.value, self.has_data, self.flagged_days) == (
            other.value, other.has_data, other.flagged_days)

    def __repr__(self):
        return "Score(value={!r}, has_data={!r}, flagged_days={!r})".format(
            self.value, self.has_data, self.flagged_days)


def _verified(day, metric, manual_field):
    """The day's total minus anything typed in by hand."""
    return max(0.0, float(getattr(day, metric)) - float(getattr(day, manual_field) or 0))


def _merged_duration(intervals, cap=MAX_SESSION):
    """Total time covered by the intervals, counting overlaps once; each interval is first cut to `cap`."""
    clipped = sorted((start, min(end, start + cap)) for start, end in intervals if end > start)
    total = timedelta()
    current_start = current_end = None
    for start, end in clipped:
        if current_end is None or start > current_end:
            if current_end is not None:
                total += current_end - current_start
            current_start, current_end = start, end
        else:
            current_end = max(current_end, end)
    if current_end is not None:
        total += current_end - current_start
    return total


def _days(challenge, enrollment, source, status_key):
    return DailyActivity.objects.filter(
        user=enrollment.user, source=source,
        local_date__gt=enrollment.joined_local_date,
        local_date__gte=challenge.start_date, local_date__lte=challenge.end_date,
        **{f"metric_status__{status_key}": "available"},
    )


def _sessions(challenge, enrollment, source, types=None, minimum=MIN_SESSION):
    rows = ExerciseRecord.objects.filter(
        user=enrollment.user, source=source,
        local_date__gte=challenge.start_date, local_date__lte=challenge.end_date,
        start_at__gte=enrollment.joined_at,
    ).exclude(recording_method="manual")
    if types is not None:
        rows = rows.filter(exercise_type__in=types)
    return [(r.start_at, r.end_at) for r in rows if r.end_at - r.start_at >= minimum]


def score(challenge, enrollment, source):
    """Progress for one enrollment, in the challenge's own unit."""
    kind = challenge.kind
    result = Score()
    if source is None:
        return result

    if kind in ("steps", "distance", "calories", "step_days"):
        if kind == "distance":
            metric, manual, status, cap, scale = "distance_m", "manual_distance_m", "distance_m", DAILY_CAPS["distance_km"], 1000.0
        elif kind == "calories":
            metric, manual, status, cap, scale = "active_kcal", "manual_active_kcal", "active_kcal", DAILY_CAPS["calories"], 1.0
        else:
            metric, manual, status, cap, scale = "steps", "manual_steps", "steps", DAILY_CAPS["steps"], 1.0
        total = 0.0
        good_days = 0
        for day in _days(challenge, enrollment, source, status).exclude(**{f"{metric}__isnull": True}):
            result.has_data = True
            amount = _verified(day, metric, manual) / scale
            if amount > cap:
                result.flagged_days += 1
                amount = cap
            total += amount
            if kind == "step_days" and amount >= challenge.daily_threshold:
                good_days += 1
        if kind == "step_days":
            result.value = good_days
        elif kind == "distance":
            result.value = round(total, 1)
        else:
            result.value = int(total)
        return result

    if kind == "sleep_nights":
        nights = {}
        rows = SleepRecord.objects.filter(
            user=enrollment.user, source=source,
            local_date__gt=enrollment.joined_local_date,
            local_date__gte=challenge.start_date, local_date__lte=challenge.end_date,
        ).exclude(recording_method="manual")
        for row in rows:
            nights.setdefault(row.local_date, []).append((row.start_at, row.end_at))
        result.has_data = bool(nights)
        need = timedelta(minutes=challenge.daily_threshold)
        result.value = sum(1 for spans in nights.values() if _merged_duration(spans, MAX_SLEEP_NIGHT) >= need)
        return result

    if kind == "running":
        sessions = _sessions(challenge, enrollment, source, RUNNING_TYPES, minimum=timedelta(minutes=1))
    elif kind == "workout":
        sessions = _sessions(challenge, enrollment, source, ACTIVITY_TYPES.get(challenge.activity_type, ()))
    elif kind == "active_minutes":
        sessions = _sessions(challenge, enrollment, source)
    else:
        return result
    result.has_data = bool(sessions)
    result.value = int(_merged_duration(sessions).total_seconds() // 60)
    return result
