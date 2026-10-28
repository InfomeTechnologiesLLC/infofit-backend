"""Proactive coaching automations.

Design rules (same as IVA): verified facts only, opt-in, at most DAILY_CAP automated messages per person
per local day, neutral wording, and nothing about weight or body measurements in any message.
Rules decide *what* to say; the AI model (when configured) only words it, with a fixed template fallback.
"""
import json
import logging
import statistics
from datetime import timedelta
from backports.zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from accounts.models import ADMIN_ROLES, Role, User
from ai_service.coaching import COACH_SYSTEM, daily_facts
from ai_service.provider import complete
from challenges.models import Challenge, ChallengeEnrollment
from fitness.models import DailyActivity, FitnessAccount
from notifications.models import Notification
from notifications.services import create_notification
from nutrition.models import Meal
from wellness.models import BodyMeasurement, HydrationLog, ReminderSettings

from .models import AutomationLog, AutomationSettings, StepGoalSuggestion

log = logging.getLogger("infome.automations")

DAILY_CAP = 2          # automated coaching messages per person per local day, across all kinds
SLOT_MINUTES = 5       # the scheduler runs every 5 minutes, so a send window is 5 minutes wide
MIN_COHORT = 5         # smallest group any HR figure may describe

REVIEW_SYSTEM = (
    "You are IVA, a brief wellness coach writing a weekly review. Use only the supplied verified facts. "
    "Write two or three warm sentences: celebrate the win, then give the one small goal for this week. "
    "Never mention weight, body measurements or BMI. Do not invent data, diagnoses or targets, do not scold, "
    "and do not infer inactivity from missing data."
)


# ---- shared helpers --------------------------------------------------------------------------

def user_zone(user):
    name = (ReminderSettings.objects.filter(user=user).values_list("time_zone", flat=True).first()
            or FitnessAccount.objects.filter(user=user).values_list("time_zone", flat=True).first() or "UTC")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _due(queryset, now):
    """Yield (settings, local_datetime) for people whose chosen send time falls in the current slot."""
    for s in queryset.select_related("user").iterator():
        local = now.astimezone(user_zone(s.user))
        minute = local.hour * 60 + local.minute
        if s.send_minute <= minute < s.send_minute + SLOT_MINUTES:
            yield s, local


def can_send(user, kind, local_date):
    logs = AutomationLog.objects.filter(user=user, local_date=local_date)
    return not logs.filter(kind=kind).exists() and logs.count() < DAILY_CAP


def mark_sent(user, kind, local_date):
    AutomationLog.objects.get_or_create(user=user, kind=kind, local_date=local_date)


def ai_text(system, facts, fallback, instruction):
    """Ask the model to word the message; any failure or an unconfigured key falls back to the template."""
    try:
        text = complete([{"role": "system", "content": system},
                         {"role": "user", "content": instruction + json.dumps(facts)}], settings.GEMINI_MODEL)
        text = " ".join(str(text).split())
        if text:
            return text[:230]
    except Exception:  # network, quota, bad key, safety block: the nudge must still go out
        log.warning("Automation wording unavailable; using template.")
    return fallback


def _goal(user):
    profile = getattr(user, "profile", None)
    return profile.daily_step_goal if profile else 10000


def _step_rows(user, start, end):
    """(local_date, steps) for days with verified step data from the active source."""
    account = FitnessAccount.objects.filter(user=user).select_related("active_source").first()
    if not account or not account.active_source_id:
        return []
    rows = DailyActivity.objects.filter(user=user, source=account.active_source, local_date__range=(start, end))
    return sorted((r.local_date, r.steps) for r in rows
                  if r.metric_status.get("steps") == "available" and r.steps is not None)


# ---- 1. daily coach nudge ---------------------------------------------------------------------

DAILY_FALLBACK = "Good morning. A short walk or a glass of water is an easy way to start today."


def send_daily_coach(now=None):
    now = now or timezone.now()
    sent = 0
    for s, local in _due(AutomationSettings.objects.filter(daily_coach_enabled=True, user__is_active=True), now):
        day = local.date()
        if not can_send(s.user, "daily_coach", day):
            continue
        facts = daily_facts(s.user)["facts"]
        text = ai_text(COACH_SYSTEM, facts, DAILY_FALLBACK, "Write a brief coaching suggestion using these facts as data: ")
        with transaction.atomic():
            create_notification(s.user, Notification.Kind.COACH_NUDGE, "Your daily coach", text, f"automation:daily:{day}")
            mark_sent(s.user, "daily_coach", day)
        sent += 1
    return sent


# ---- 2. weekly review -------------------------------------------------------------------------

def review_facts(user, today):
    """Verified facts about the previous Monday-Sunday, plus one win and one small goal chosen by rules."""
    start = today - timedelta(days=today.weekday() + 7)
    end = start + timedelta(days=6)
    goal = _goal(user)
    rows = _step_rows(user, start, end)
    days_met = sum(1 for _, steps in rows if steps >= goal)
    water_days = HydrationLog.objects.filter(user=user, local_date__range=(start, end)).values("local_date").distinct().count()
    meal_days = Meal.objects.filter(user=user, local_date__range=(start, end)).values("local_date").distinct().count()
    checked_in = BodyMeasurement.objects.filter(user=user, local_date__range=(start, end)).exists()

    if days_met >= 1:
        win = f"reached the daily step goal on {days_met} day{'s' if days_met != 1 else ''}"
    elif water_days >= 3:
        win = f"logged water on {water_days} days"
    elif meal_days >= 3:
        win = f"saved meals on {meal_days} days"
    elif checked_in:
        win = "kept up the weekly check-in"
    else:
        win = "started a fresh week, and any week is a good time to begin"
    next_goal = (f"try to reach the daily step goal on {min(7, days_met + 1)} days" if rows
                 else "sync your steps so progress can be tracked")
    facts = [
        f"Step data was recorded on {len(rows)} of 7 days last week." if rows else "No step data synced last week.",
        f"Win to celebrate: the employee {win}.",
        f"One small goal for this week: {next_goal}.",
    ]
    return {"facts": facts, "fallback": f"Last week you {win}. This week, {next_goal}."}


def send_weekly_reviews(now=None):
    now = now or timezone.now()
    sent = 0
    for s, local in _due(AutomationSettings.objects.filter(weekly_review_enabled=True, user__is_active=True), now):
        day = local.date()
        if day.weekday() != 0 or not can_send(s.user, "weekly_review", day):
            continue
        data = review_facts(s.user, day)
        text = ai_text(REVIEW_SYSTEM, data["facts"], data["fallback"], "Write the weekly review using these facts as data: ")
        year, week, _ = day.isocalendar()
        with transaction.atomic():
            create_notification(s.user, Notification.Kind.WEEKLY_REVIEW, "Your weekly review", text,
                                f"automation:weekly:{year}-W{week}")
            mark_sent(s.user, "weekly_review", day)
        sent += 1
    return sent


# ---- 3. adaptive step goal (rules only, suggestion only) --------------------------------------

def _round500(value):
    return int(round(value / 500.0) * 500)


def propose_step_goal(user, today):
    """Return (suggested_goal, reason) or None. Needs 5+ days of data in each of the last two full weeks."""
    goal = _goal(user)
    this_monday = today - timedelta(days=today.weekday())
    weeks = []
    for back in (14, 7):
        start = this_monday - timedelta(days=back)
        rows = _step_rows(user, start, start + timedelta(days=6))
        if len(rows) < 5:
            return None
        weeks.append(sum(1 for _, steps in rows if steps >= goal))
    if all(m >= 5 for m in weeks):
        suggested = min(_round500(goal * 1.10), 20000)
        if suggested > goal:
            return suggested, "You reached your goal on most days for two weeks in a row, so you may be ready for a slightly higher one."
    if all(m <= 1 for m in weeks):
        suggested = max(_round500(goal * 0.85), 3000)
        if suggested < goal:
            return suggested, "Your goal has been tough to reach lately. A smaller target can be easier to keep up, and you can raise it later."
    return None


def create_step_goal_suggestions(now=None):
    now = now or timezone.now()
    created = 0
    for user in User.objects.filter(is_active=True, fitness_account__active_source__isnull=False).select_related("profile").iterator():
        today = now.astimezone(user_zone(user)).date()
        if today.weekday() != 0:
            continue
        # Never nag: skip if any suggestion exists from the last 28 days.
        if StepGoalSuggestion.objects.filter(user=user, created_at__gte=now - timedelta(days=28)).exists():
            continue
        proposal = propose_step_goal(user, today)
        if not proposal:
            continue
        suggested, reason = proposal
        _, was_created = StepGoalSuggestion.objects.get_or_create(
            user=user, week_start=today,
            defaults={"current_goal": _goal(user), "suggested_goal": suggested, "reason": reason})
        created += int(was_created)
    return created


# ---- 4. smart reminder timing (statistics only) -----------------------------------------------

def suggest_water_reminder_start(user, now=None):
    """Median first-water-log time over 21 days, minus 15 minutes. Needs 7+ days of logs."""
    now = now or timezone.now()
    zone = user_zone(user)
    firsts = {}
    for created in HydrationLog.objects.filter(user=user, created_at__gte=now - timedelta(days=21)).values_list("created_at", flat=True):
        local = created.astimezone(zone)
        minute = local.hour * 60 + local.minute
        firsts[local.date()] = min(minute, firsts.get(local.date(), minute))
    if len(firsts) < 7:
        return None
    start = max(0, int(statistics.median(firsts.values())) - 15)
    start = start // 15 * 15
    current = ReminderSettings.objects.filter(user=user).values_list("start_minute", flat=True).first()
    return {"suggested_start_minute": start, "based_on_days": len(firsts), "current_start_minute": current}


# ---- 5. re-engagement -------------------------------------------------------------------------

REENGAGE_TEXT = "No pressure, we're here whenever you're ready. A short walk or a glass of water is a fine way to start again."


def _last_activity(user):
    stamps = [
        DailyActivity.objects.filter(user=user).order_by("-synced_at").values_list("synced_at", flat=True).first(),
        HydrationLog.objects.filter(user=user).order_by("-created_at").values_list("created_at", flat=True).first(),
        Meal.objects.filter(user=user).order_by("-created_at").values_list("created_at", flat=True).first(),
    ]
    stamps = [s for s in stamps if s]
    return max(stamps) if stamps else user.date_joined


def send_reengagement(now=None):
    now = now or timezone.now()
    sent = 0
    for s, local in _due(AutomationSettings.objects.filter(reengagement_enabled=True, user__is_active=True), now):
        day = local.date()
        if now - _last_activity(s.user) < timedelta(days=3):
            continue
        if AutomationLog.objects.filter(user=s.user, kind="reengagement", local_date__gte=day - timedelta(days=6)).exists():
            continue  # at most one a week
        if not can_send(s.user, "reengagement", day):
            continue
        with transaction.atomic():
            create_notification(s.user, Notification.Kind.REENGAGEMENT, "We saved your spot", REENGAGE_TEXT,
                                f"automation:reengage:{day}")
            mark_sent(s.user, "reengagement", day)
        sent += 1
    return sent


# ---- 6. challenge suggestions (rules only) ----------------------------------------------------

def suggest_challenges(user, today, limit=3):
    rows = _step_rows(user, today - timedelta(days=13), today)
    if len(rows) < 5:
        return []
    average = sum(steps for _, steps in rows) / len(rows)
    if average <= 0:
        return []
    joined = set(ChallengeEnrollment.objects.filter(user=user).values_list("challenge_id", flat=True))
    found = []
    for c in Challenge.objects.filter(is_published=True, kind=Challenge.Kind.STEPS, end_date__gt=today).exclude(id__in=joined):
        remaining = (c.end_date - max(today, c.start_date - timedelta(days=1))).days
        if remaining <= 0 or not c.target_steps:
            continue
        needed = c.target_steps / remaining
        ratio = needed / average
        if ratio > 1.3:
            continue  # out of reach right now; do not suggest it
        found.append((ratio, {
            "challenge_id": c.id, "title": c.title, "fit": "comfortable" if ratio <= 1.0 else "stretch",
            "needed_steps_per_day": int(round(needed)), "recent_average_steps": int(round(average)),
            "reason": (f"About {int(round(needed)):,} steps a day would complete it, and you have averaged "
                       f"about {int(round(average)):,} recently."),
        }))
    found.sort(key=lambda pair: (0, -pair[0]) if pair[0] <= 1.0 else (1, pair[0]))  # comfortable fits first, then stretches
    return [item for _, item in found[:limit]]


# ---- 7. HR participation digest (aggregates only) ---------------------------------------------

def hr_digest(now=None):
    now = now or timezone.now()
    since = now - timedelta(days=7)
    staff = list(User.objects.filter(is_active=True, role__in=[Role.EMPLOYEE, Role.TEAM_MANAGER])
                 .values_list("id", "profile__department__name"))
    if len(staff) < MIN_COHORT:
        return {"available": False, "reason": f"Fewer than {MIN_COHORT} employees, so no figures are shown."}
    active = (set(DailyActivity.objects.filter(synced_at__gte=since).values_list("user_id", flat=True))
              | set(HydrationLog.objects.filter(created_at__gte=since).values_list("user_id", flat=True))
              | set(Meal.objects.filter(created_at__gte=since).values_list("user_id", flat=True)))
    ids = {uid for uid, _ in staff}
    active &= ids
    groups = {}
    for uid, dept in staff:
        groups.setdefault(dept or "No department", []).append(uid)
    departments, pooled = [], []
    for name, members in sorted(groups.items()):
        if len(members) >= MIN_COHORT:
            departments.append({"name": name, "employees": len(members), "active": len(active & set(members))})
        else:
            pooled += members
    if len(pooled) >= MIN_COHORT:  # small groups are only shown merged, and only if the merged group is large enough
        departments.append({"name": "Other departments", "employees": len(pooled), "active": len(active & set(pooled))})
    in_challenge = (ChallengeEnrollment.objects.filter(user_id__in=ids, challenge__is_published=True, challenge__end_date__gte=now.date())
                    .values("user_id").distinct().count())
    return {"available": True, "period_days": 7, "total_employees": len(ids), "active_employees": len(active),
            "participation_percent": round(100 * len(active) / len(ids)), "in_active_challenge": in_challenge,
            "departments": departments}


def send_hr_digests(now=None):
    now = now or timezone.now()
    sent = 0
    admins = User.objects.filter(is_active=True, role__in=ADMIN_ROLES)
    digest = None
    for admin in admins.iterator():
        prefs = AutomationSettings.objects.filter(user=admin).first()
        if prefs and not prefs.hr_digest_enabled:
            continue
        local = now.astimezone(user_zone(admin))
        minute = local.hour * 60 + local.minute
        send_minute = prefs.send_minute if prefs else 540
        if local.weekday() != 0 or not (send_minute <= minute < send_minute + SLOT_MINUTES):
            continue
        digest = digest or hr_digest(now)
        body = (f"{digest['active_employees']} of {digest['total_employees']} employees ({digest['participation_percent']}%) "
                "logged activity in the last 7 days. Open the digest for the department view."
                if digest["available"] else "Not enough participants for a privacy-safe digest yet.")
        year, week, _ = local.date().isocalendar()
        _, created = create_notification(admin, Notification.Kind.HR_DIGEST, "Weekly participation digest", body,
                                         f"automation:hrdigest:{year}-W{week}", push=False)
        sent += int(created)
    return sent
