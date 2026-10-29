"""Verified, user-scoped facts supplied to IVA coaching responses."""
import json
from datetime import timedelta
try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # Python 3.8 hosts
    from backports.zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db.models import Count, Sum
from django.utils import timezone
from rest_framework.exceptions import NotFound

from challenges.models import ChallengeEnrollment
from fitness.models import DailyActivity, FitnessAccount
from nutrition.models import Meal
from wellness.models import HydrationLog, ProgramCheckIn, ProgramEnrollment, ReminderSettings

from .provider import complete


COACH_SYSTEM = (
    "You are IVA, a brief wellness coach. Use only the supplied verified facts. "
    "Write one or two supportive sentences with one practical next action. "
    "Do not invent measurements, activity types, targets, health diagnoses, or missing records. "
    "Do not infer sitting from absent steps or running from steps. "
    "Do not prescribe a water amount or a medical treatment. "
    "Do not repeat numbers; the app shows verified numbers separately."
)


def _today(user):
    settings = ReminderSettings.objects.filter(user=user).first()
    account = FitnessAccount.objects.filter(user=user).select_related("active_source").first()
    name = settings.time_zone if settings else (account.time_zone if account else "UTC")
    try:
        zone = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo("UTC")
    return timezone.now().astimezone(zone).date(), account


def _active_challenges(user, today):
    return ChallengeEnrollment.objects.filter(
        user=user, challenge__is_published=True,
        challenge__start_date__lte=today, challenge__end_date__gte=today,
    ).select_related("challenge", "progress").order_by("joined_at")


def _challenge_fact(enrollment):
    challenge = enrollment.challenge
    progress = getattr(enrollment, "progress", None)
    label = challenge.progress_label(progress)
    if not label:
        return f"{challenge.title} (goal {challenge.target_label}): no recorded progress has synced since joining."
    return f"{challenge.title} (goal {challenge.target_label}): {label} so far."


def daily_facts(user):
    today, account = _today(user)
    facts = []
    day = (DailyActivity.objects.filter(user=user, source=account.active_source, local_date=today).first()
           if account and account.active_source_id else None)
    if day and day.metric_status.get("steps") == "available" and day.steps is not None:
        facts.append(f"Recorded steps today: {day.steps}.")
    else:
        facts.append("Today's steps have not synced.")
    water = HydrationLog.objects.filter(user=user, local_date=today).aggregate(total=Sum("volume_ml"), count=Count("id"))
    facts.append(f"Water logged today: {water['total']} ml (self-reported)." if water["count"] else
                 "No water has been logged today; this does not mean none was consumed.")
    meals = Meal.objects.filter(user=user, local_date=today).count()
    facts.append(f"Saved meals today: {meals}." if meals else "No meals have been saved today.")
    programs = ProgramEnrollment.objects.filter(user=user, program__start_date__lte=today,
                                                program__end_date__gte=today).count()
    checkins = ProgramCheckIn.objects.filter(enrollment__user=user, local_date=today).count()
    if programs:
        facts.append(f"Wellness programs checked in today: {checkins} of {programs} joined programs.")
    for enrollment in _active_challenges(user, today)[:2]:
        facts.append(_challenge_fact(enrollment))
    return {"period": "daily", "from": today.isoformat(), "to": today.isoformat(), "facts": facts}


def weekly_facts(user):
    today, account = _today(user)
    start = today - timedelta(days=today.weekday())
    previous_start = start - timedelta(days=7)
    previous_end = previous_start + (today - start)
    facts = []
    if not account or not account.active_source_id:
        facts.append("No active Health Connect source is connected.")
    else:
        rows = DailyActivity.objects.filter(user=user, source=account.active_source,
                                            local_date__range=(previous_start, today))
        current = [row for row in rows if start <= row.local_date <= today and
                   row.metric_status.get("steps") == "available" and row.steps is not None]
        previous = [row for row in rows if previous_start <= row.local_date <= previous_end and
                    row.metric_status.get("steps") == "available" and row.steps is not None]
        days = (today - start).days + 1
        facts.append(f"This week: {sum(row.steps for row in current)} recorded steps across {len(current)} of {days} elapsed days."
                     if current else "No step data has synced this week.")
        facts.append(f"Same days last week: {sum(row.steps for row in previous)} recorded steps across {len(previous)} of {days} days."
                     if previous else "No step data was recorded for the same days last week.")
        if len(current) == days and len(previous) == days:
            delta = sum(row.steps for row in current) - sum(row.steps for row in previous)
            facts.append(f"Week-over-week step difference for matched complete days: {delta:+d}.")
        else:
            facts.append("A full week-over-week comparison is unavailable because some days lack data.")
    return {"period": "week", "from": start.isoformat(), "to": today.isoformat(), "facts": facts}


def challenge_facts(user, challenge_id):
    today, _ = _today(user)
    enrollment = ChallengeEnrollment.objects.filter(user=user, challenge_id=challenge_id).select_related(
        "challenge", "progress").first()
    if not enrollment:
        raise NotFound("Joined challenge not found.")
    challenge = enrollment.challenge
    facts = [f"Challenge: {challenge.title}.", _challenge_fact(enrollment)]
    facts.append(challenge.counting_note)
    progress = getattr(enrollment, "progress", None)
    facts.append("Challenge complete." if progress and progress.is_complete else "Challenge in progress.")
    if today > challenge.end_date:
        facts.append("The challenge has ended.")
    return {"period": "challenge", "from": challenge.start_date.isoformat(),
            "to": challenge.end_date.isoformat(), "facts": facts}


def generate_coaching(snapshot, model):
    prompt = "Write a brief coaching suggestion using these facts as data: " + json.dumps(snapshot["facts"])
    return complete([{"role": "system", "content": COACH_SYSTEM},
                     {"role": "user", "content": prompt}], model)
