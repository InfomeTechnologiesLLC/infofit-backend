"""Company rankings for the app: steps (today, this week, this month), challenge points and teams.

Names are shown as "First L."; anyone can hide their name (they are then shown as "Colleague" but still
ranked). Steps come only from each person's active phone. Legacy Android boards use synced totals;
Apple Health boards use the separately normalized non-manual totals.
"""
from django.db.models import F, Sum
from django.utils import timezone

from accounts.models import Role, User
from fitness.models import DailyActivity

from .models import PointsLedger

BOARDS = {
    "steps_today": ("Steps today", "steps"),
    "steps_week": ("Steps this week", "steps"),
    "steps_month": ("Steps this month", "steps"),
    "points": ("Challenge points", "points"),
    "teams": ("Teams: average steps this week", "avg steps"),
}
TOP = 50
NOTE = ("Steps update whenever someone's phone syncs: when they open the app, and about every hour in the "
        "background. Only steps recorded by each person's active phone or watch count.")


def period(board, today):
    if board == "steps_today":
        return today, today
    if board == "steps_month":
        return today.replace(day=1), today
    from datetime import timedelta
    return today - timedelta(days=today.weekday()), today  # week starts on Monday


def display_name(user, me):
    if user.pk == me.pk:
        return "You"
    profile = getattr(user, "profile", None)
    if profile is not None and not profile.show_in_rankings:
        return "Colleague"
    first, last = (user.first_name or "").strip(), (user.last_name or "").strip()
    if not first:
        return "Colleague"
    return "{} {}.".format(first, last[:1].upper()) if last else first


def _people():
    return User.objects.filter(is_active=True, role__in=[Role.EMPLOYEE, Role.TEAM_MANAGER]).select_related("profile__team")


def _steps(user_ids, start, end):
    rows = (DailyActivity.objects.filter(user_id__in=user_ids, source_id=F("user__fitness_account__active_source_id"),
                                         steps__isnull=False, local_date__gte=start, local_date__lte=end)
            .only("user_id", "steps", "verified_metrics"))
    totals = {}
    for row in rows:
        # Preserve the established Android leaderboard totals during the iOS rollout.
        value = row.verified_metrics.get("steps", row.steps)
        if value is not None:
            totals[row.user_id] = totals.get(row.user_id, 0) + value
    return totals


def _fmt(value, unit):
    return "{:,} {}".format(int(round(value)), unit)


def board_payload(me, board, today=None):
    today = today or timezone.localdate()
    title, unit = BOARDS[board]
    people = {u.pk: u for u in _people()}
    if me.pk not in people:
        people[me.pk] = me
    if board == "teams":
        start, end = period("steps_week", today)
        steps = _steps(list(people), start, end)
        teams = {}
        for pk, steps_total in steps.items():
            profile = getattr(people[pk], "profile", None)
            if profile is None or profile.team_id is None:
                continue
            entry = teams.setdefault(profile.team_id, {"name": profile.team.name, "total": 0, "members": 0})
            entry["total"] += steps_total
            entry["members"] += 1
        my_team = getattr(getattr(me, "profile", None), "team_id", None)
        ordered = sorted(teams.items(), key=lambda kv: (-kv[1]["total"] / kv[1]["members"], kv[0]))
        rows = [{"rank": i, "name": t["name"], "detail": "{} with steps this week".format(
                     "1 member" if t["members"] == 1 else "{} members".format(t["members"])),
                 "value": round(t["total"] / t["members"]), "value_label": _fmt(t["total"] / t["members"], unit),
                 "is_me": team_id == my_team}
                for i, (team_id, t) in enumerate(ordered, 1)]
        me_row = next((r for r in rows if r["is_me"]), None)
        return _wrap(board, title, unit, rows[:TOP], me_row, len(rows), today,
                     "" if my_team else "You are not in a team yet. Ask HR to add you to one.")
    if board == "points":
        values = {row["user_id"]: row["total"] or 0 for row in
                  PointsLedger.objects.filter(user_id__in=list(people)).values("user_id").annotate(total=Sum("delta"))}
        values = {pk: v for pk, v in values.items() if v > 0}
        empty = "No points yet. Complete a challenge to get on the board."
    else:
        start, end = period(board, today)
        values = {pk: v for pk, v in _steps(list(people), start, end).items() if v > 0}
        empty = "No synced steps for you in this period yet. Open the app to sync."
    ordered = sorted(values.items(), key=lambda kv: (-kv[1], kv[0]))
    rows = []
    for i, (pk, value) in enumerate(ordered, 1):
        user = people[pk]
        profile = getattr(user, "profile", None)
        team = profile.team.name if profile is not None and profile.team_id else ""
        rows.append({"rank": i, "name": display_name(user, me), "detail": team, "value": value,
                     "value_label": _fmt(value, unit), "is_me": pk == me.pk})
    me_row = next((r for r in rows if r["is_me"]), None)
    return _wrap(board, title, unit, rows[:TOP], me_row, len(rows), today, "" if me_row else empty)


def _wrap(board, title, unit, rows, me_row, count, today, message):
    return {"board": board, "title": title, "unit": unit, "local_date": today, "updated_at": timezone.now(),
            "participants": count, "rows": rows, "me": me_row, "message": message, "note": NOTE}
