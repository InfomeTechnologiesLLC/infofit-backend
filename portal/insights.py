"""Staff-only participation and verified step summaries.

Steps come from the employee's active Health Connect source. Missing readings are
not treated as zero, and the rankings are activity counts rather than health scores.
"""
from datetime import timedelta

from django.db.models import Count, F, Sum
from django.utils import timezone

from accounts.models import Role, User
from challenges.models import ChallengeEnrollment, ChallengeProgress, PointsLedger
from fitness.models import DailyActivity


def step_period(period, today=None):
    today = today or timezone.localdate()
    if period == "week":
        return today - timedelta(days=today.weekday()), today
    if period == "month":
        return today.replace(day=1), today
    if period == "today":
        return today, today
    return None, None


def step_totals(user_ids, start=None, end=None):
    user_ids = list(user_ids)
    if not user_ids:
        return {}
    rows = DailyActivity.objects.filter(
        user_id__in=user_ids, source_id=F("user__fitness_account__active_source_id"),
        steps__isnull=False,
    )
    if start is not None:
        rows = rows.filter(local_date__gte=start)
    if end is not None:
        rows = rows.filter(local_date__lte=end)
    return {row["user_id"]: {"steps": row["steps"] or 0, "days": row["days"]}
            for row in rows.values("user_id").annotate(steps=Sum("steps"), days=Count("id"))}


def points_totals(user_ids):
    rows = PointsLedger.objects.filter(user_id__in=user_ids).values("user_id").annotate(total=Sum("delta"))
    return {row["user_id"]: row["total"] or 0 for row in rows}


def completion_totals(user_ids):
    rows = ChallengeProgress.objects.filter(
        enrollment__user_id__in=user_ids, is_complete=True,
    ).values("enrollment__user_id").annotate(total=Count("id"))
    return {row["enrollment__user_id"]: row["total"] for row in rows}


def joined_totals(user_ids):
    rows = ChallengeEnrollment.objects.filter(user_id__in=user_ids).values("user_id").annotate(total=Count("id"))
    return {row["user_id"]: row["total"] for row in rows}


def staff_rankings(sort="points", period="month"):
    employees = list(User.objects.filter(role=Role.EMPLOYEE, is_active=True).select_related("profile__team"))
    ids = [employee.pk for employee in employees]
    start, end = step_period(period)
    steps = step_totals(ids, start, end)
    points = points_totals(ids)
    completions = completion_totals(ids)
    joined = joined_totals(ids)
    rows = [{"user": employee, "points": points.get(employee.pk, 0),
             "steps": steps.get(employee.pk, {}).get("steps"),
             "step_days": steps.get(employee.pk, {}).get("days", 0),
             "completed": completions.get(employee.pk, 0),
             "joined": joined.get(employee.pk, 0)} for employee in employees]
    field = {"points": "points", "steps": "steps", "challenges": "completed"}.get(sort, "points")
    rows.sort(key=lambda row: (-(row[field] or 0), row["user"].pk))
    for rank, row in enumerate(rows, 1):
        row["rank"] = rank
    return rows
