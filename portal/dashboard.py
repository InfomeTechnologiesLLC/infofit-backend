"""Figures for the staff dashboard. Everything is computed from saved app records at request time."""
from datetime import timedelta

from django.db.models import Count, F, Max, Q, Sum
from django.utils import timezone

from accounts.models import Department, Role, User
from challenges.models import Challenge, ChallengeEnrollment, ChallengeProgress, PointsLedger
from community.models import CommunityPost, CommunityReport
from fitness.models import DailyActivity, FitnessSource
from notifications.models import PushDelivery
from nutrition.models import Meal
from wellness.models import HydrationLog, ProgramCheckIn, ProgramEnrollment, WellnessProgram

KIND_ICONS = {
    "steps": "bi-person-walking", "running": "bi-lightning-charge", "distance": "bi-signpost-split",
    "active_minutes": "bi-stopwatch", "workout": "bi-bicycle", "calories": "bi-fire",
    "step_days": "bi-calendar-check", "sleep_nights": "bi-moon-stars",
}


def _pct(part, whole):
    return round(100 * part / whole) if whole else 0


def _employees():
    return User.objects.filter(role=Role.EMPLOYEE, is_active=True)


def _active_source_days(start, end):
    """Synced days from each employee's active phone, with steps recorded."""
    return DailyActivity.objects.filter(
        user__role=Role.EMPLOYEE, user__is_active=True,
        source_id=F("user__fitness_account__active_source_id"),
        local_date__range=(start, end), steps__isnull=False,
    )


def kpis(today):
    employees = _employees()
    total = employees.count()
    week_ago = timezone.now() - timedelta(days=7)
    new_this_week = employees.filter(date_joined__gte=week_ago).count()
    challenge_now = set(ChallengeEnrollment.objects.filter(
        user__in=employees, challenge__is_published=True,
        challenge__start_date__lte=today, challenge__end_date__gte=today).values_list("user_id", flat=True))
    program_now = set(ProgramEnrollment.objects.filter(
        user__in=employees, program__is_published=True,
        program__start_date__lte=today, program__end_date__gte=today).values_list("user_id", flat=True))
    synced_week = set(_active_source_days(today - timedelta(days=6), today).values_list("user_id", flat=True))
    checked_in = set(ProgramCheckIn.objects.filter(
        enrollment__user__in=employees, created_at__gte=week_ago).values_list("enrollment__user_id", flat=True))
    engaged = synced_week | checked_in
    today_rows = _active_source_days(today, today).aggregate(steps=Sum("steps"), people=Count("user", distinct=True))
    week = _active_source_days(today - timedelta(days=6), today).aggregate(steps=Sum("steps"), days=Count("id"))
    month_start = today.replace(day=1)
    completions_month = PointsLedger.objects.filter(reason="award", created_at__date__gte=month_start).count()
    live = Challenge.objects.filter(is_published=True, start_date__lte=today, end_date__gte=today).count()
    review = CommunityPost.objects.filter(status=CommunityPost.Status.REVIEW).count()
    reports = CommunityReport.objects.filter(status=CommunityReport.Status.OPEN).count()
    programs = WellnessProgram.objects.filter(is_published=True, owner__isnull=True, start_date__lte=today,
                                              end_date__gte=today).count()
    return [
        {"label": "Active employees", "value": total, "icon": "bi-people", "tone": "teal",
         "note": f"+{new_this_week} new this week" if new_this_week else "No new sign-ups this week"},
        {"label": "Taking part now", "value": len(challenge_now | program_now), "icon": "bi-trophy", "tone": "amber",
         "note": f"{_pct(len(challenge_now | program_now), total)}% in a live challenge or program"},
        {"label": "Active in last 7 days", "value": len(engaged), "icon": "bi-activity", "tone": "violet",
         "note": f"{_pct(len(engaged), total)}% synced steps or checked in"},
        {"label": "Steps today", "value": f"{today_rows['steps'] or 0:,}", "icon": "bi-person-walking", "tone": "cyan",
         "note": f"from {today_rows['people'] or 0} employee{'s' if today_rows['people'] != 1 else ''}"},
        {"label": "Average steps per day", "value": f"{int((week['steps'] or 0) / week['days']) if week['days'] else 0:,}",
         "icon": "bi-graph-up", "tone": "green", "note": "last 7 days, per synced day"},
        {"label": "Live challenges", "value": live, "icon": "bi-flag", "tone": "blue",
         "note": f"{completions_month} completion{'s' if completions_month != 1 else ''} this month"},
        {"label": "Needs review", "value": review + reports, "icon": "bi-shield-exclamation",
         "tone": "red" if review + reports else "green",
         "note": f"{review} held post{'s' if review != 1 else ''}, {reports} open report{'s' if reports != 1 else ''}"},
        {"label": "Wellness check-ins today",
         "value": ProgramCheckIn.objects.filter(local_date=today).count(), "icon": "bi-heart-pulse", "tone": "pink",
         "note": f"{programs} program{'s' if programs != 1 else ''} running"},
    ]


def activity_series(today, days=30):
    """Per day: verified steps in total and how many employees synced, for the trend chart."""
    start = today - timedelta(days=days - 1)
    rows = {row["local_date"]: row for row in _active_source_days(start, today).values("local_date").annotate(
        steps=Sum("steps"), manual=Sum("manual_steps"), people=Count("user", distinct=True))}
    labels, steps, people = [], [], []
    for offset in range(days):
        day = start + timedelta(days=offset)
        row = rows.get(day)
        labels.append(day.strftime("%d %b"))
        steps.append(max(0, (row["steps"] or 0) - (row["manual"] or 0)) if row else 0)
        people.append(row["people"] if row else 0)
    return {"labels": labels, "steps": steps, "people": people}


def signup_series(today, weeks=8):
    monday = today - timedelta(days=today.weekday())
    labels, counts = [], []
    for back in range(weeks - 1, -1, -1):
        start = monday - timedelta(weeks=back)
        labels.append(start.strftime("%d %b"))
        counts.append(_employees().filter(date_joined__date__gte=start,
                                          date_joined__date__lt=start + timedelta(days=7)).count())
    return {"labels": labels, "counts": counts}


def department_participation(today):
    """Share of each department's employees who synced steps or checked in during the last 7 days."""
    week_start = today - timedelta(days=6)
    active = set(_active_source_days(week_start, today).values_list("user_id", flat=True)) | set(
        ProgramCheckIn.objects.filter(local_date__gte=week_start).values_list("enrollment__user_id", flat=True))
    rows = []
    for dept in Department.objects.order_by("name"):
        ids = set(_employees().filter(profile__department=dept).values_list("pk", flat=True))
        if ids:
            rows.append({"name": dept.name, "employees": len(ids), "percent": _pct(len(ids & active), len(ids))})
    unassigned = set(_employees().filter(profile__department__isnull=True).values_list("pk", flat=True))
    if unassigned:
        rows.append({"name": "No department", "employees": len(unassigned),
                     "percent": _pct(len(unassigned & active), len(unassigned))})
    rows.sort(key=lambda row: -row["percent"])
    return rows


def live_challenges(today):
    rows = []
    for c in Challenge.objects.filter(is_published=True, end_date__gte=today - timedelta(days=3)).annotate(
            joined=Count("enrollments", distinct=True),
            completed=Count("enrollments", filter=Q(enrollments__progress__is_complete=True), distinct=True),
            flagged=Count("enrollments", filter=Q(enrollments__progress__flagged_days__gt=0), distinct=True),
    ).order_by("end_date")[:8]:
        if c.start_date > today:
            status, tone = f"Starts {c.start_date:%d %b}", "secondary"
        elif c.end_date < today:
            status, tone = "Ended", "secondary"
        else:
            left = (c.end_date - today).days
            status, tone = ("Last day" if left == 0 else f"{left} day{'s' if left != 1 else ''} left",
                            "warning" if left <= 2 else "success")
        rows.append({"challenge": c, "icon": KIND_ICONS.get(c.kind, "bi-flag"), "status": status, "tone": tone,
                     "joined": c.joined, "completed": c.completed, "flagged": c.flagged,
                     "percent": _pct(c.completed, c.joined)})
    return rows


def attention(today):
    """Things a staff member should look at, most urgent first. Each item links to where it is fixed."""
    items = []
    review = CommunityPost.objects.filter(status=CommunityPost.Status.REVIEW).count()
    if review:
        items.append(("bi-shield-exclamation", "danger", f"{review} post{'s' if review != 1 else ''} held for review",
                      "The automatic check held them back; only the authors can see them.", "posts", "?status=review"))
    reports = CommunityReport.objects.filter(status=CommunityReport.Status.OPEN).count()
    if reports:
        items.append(("bi-flag", "danger", f"{reports} open report{'s' if reports != 1 else ''}",
                      "Colleagues reported posts.", "reports", ""))
    flagged = ChallengeProgress.objects.filter(flagged_days__gt=0, enrollment__challenge__end_date__gte=today).count()
    if flagged:
        items.append(("bi-exclamation-triangle", "warning", f"{flagged} participant{'s' if flagged != 1 else ''} with unusual days",
                      "Days above the daily limit were capped. Open the challenge to see who.", "challenges", ""))
    no_team = _employees().filter(Q(profile__team__isnull=True) | Q(profile__isnull=True)).count()
    if no_team:
        items.append(("bi-person-plus", "info", f"{no_team} employee{'s' if no_team != 1 else ''} without a team",
                      "Often new sign-ups. Team challenges and team posts need a team.", "employees", "?filter=no_team"))
    week_ago = timezone.now() - timedelta(days=7)
    synced_recently = set(FitnessSource.objects.filter(last_successful_sync_at__gte=week_ago).values_list("user_id", flat=True))
    stale = _employees().exclude(pk__in=synced_recently).count()
    if stale:
        items.append(("bi-phone", "secondary", f"{stale} employee{'s' if stale != 1 else ''} not synced in 7 days",
                      "Their steps and challenges are not updating. A reminder to open the app usually fixes it.",
                      "employees", "?filter=not_synced"))
    ending = Challenge.objects.filter(is_published=True, end_date__range=(today, today + timedelta(days=2))).count()
    if ending:
        items.append(("bi-hourglass-split", "warning", f"{ending} challenge{'s' if ending != 1 else ''} ending within 2 days",
                      "A good moment for a reminder in the community feed.", "challenges", ""))
    backlog = PushDelivery.objects.filter(state__in=[PushDelivery.State.PENDING, PushDelivery.State.RETRY],
                                          notification__created_at__lte=timezone.now() - timedelta(minutes=15)).count()
    if backlog:
        items.append(("bi-bell-slash", "warning", f"{backlog} notification{'s' if backlog != 1 else ''} stuck",
                      "Pushes waiting over 15 minutes. Check the Celery worker and Firebase settings.", "dashboard", ""))
    return items


def top_teams(limit=5):
    rows = (PointsLedger.objects.filter(user__is_active=True, user__profile__team__isnull=False)
            .values("user__profile__team__name").annotate(points=Sum("delta"), members=Count("user", distinct=True))
            .order_by("-points")[:limit])
    return [{"name": r["user__profile__team__name"], "points": r["points"] or 0, "members": r["members"]} for r in rows]


def recent_signups(limit=6):
    return _employees().select_related("profile__department", "profile__team").order_by("-date_joined")[:limit]


def today_habits(today):
    return {
        "water_people": HydrationLog.objects.filter(local_date=today).values("user").distinct().count(),
        "meal_people": Meal.objects.filter(local_date=today).values("user").distinct().count(),
        "checkins": ProgramCheckIn.objects.filter(local_date=today).count(),
        "posts_week": CommunityPost.objects.filter(created_at__gte=timezone.now() - timedelta(days=7),
                                                   status=CommunityPost.Status.PUBLISHED).count(),
    }


def last_sync_by_user(user_ids):
    return dict(FitnessSource.objects.filter(user_id__in=user_ids).values("user_id")
                .annotate(last=Max("last_successful_sync_at")).values_list("user_id", "last"))
