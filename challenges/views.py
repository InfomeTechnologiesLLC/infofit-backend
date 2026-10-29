try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python 3.8 hosts
    from backports.zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import EmployeeProfile
from fitness.models import FitnessAccount
from .models import BadgeAward, Challenge, ChallengeEnrollment, ChallengeProgress, PointsLedger
from .services import reconcile_user


def local_today(user):
    account = FitnessAccount.objects.filter(user=user).first()
    try:
        zone = ZoneInfo(account.time_zone) if account else ZoneInfo("UTC")
    except KeyError:
        zone = ZoneInfo("UTC")
    return timezone.now().astimezone(zone).date()


def challenge_payload(challenge, enrollment=None, today=None):
    progress = ChallengeProgress.objects.filter(enrollment=enrollment).first() if enrollment else None
    return {
        "id": challenge.id, "title": challenge.title, "description": challenge.description,
        "start_date": challenge.start_date, "end_date": challenge.end_date,
        "kind": challenge.kind, "target_steps": challenge.target_steps,
        "target_running_minutes": challenge.target_running_minutes,
        "reward_points": challenge.reward_points,
        "badge_title": challenge.badge_title or None, "joined": enrollment is not None,
        "can_join": bool(today and challenge.is_published and challenge.start_date <= today <= challenge.end_date),
        "verified_steps": progress.verified_steps if progress and progress.has_step_data else None,
        "verified_running_minutes": progress.verified_running_minutes if progress else 0,
        "counting_note": challenge.counting_note,
        "complete": progress.is_complete if progress else False,
        # Generic fields for every challenge kind (newer app versions use these).
        "kind_label": challenge.get_kind_display(),
        "unit": challenge.unit,
        "target": challenge.target,
        "target_label": challenge.target_label,
        "activity_type": challenge.activity_type or None,
        "daily_threshold": challenge.daily_threshold or None,
        "progress_value": progress.progress_value if progress and progress.has_step_data else None,
        "progress_label": challenge.progress_label(progress),
    }


class ChallengeListView(APIView):
    def get(self, request):
        today = local_today(request.user)
        challenges = Challenge.objects.filter(is_published=True, end_date__gte=today).order_by("start_date", "pk")
        enrollments = {e.challenge_id: e for e in ChallengeEnrollment.objects.filter(user=request.user, challenge__in=challenges)}
        return Response([challenge_payload(c, enrollments.get(c.pk), today) for c in challenges])


class JoinView(APIView):
    def post(self, request, pk):
        with transaction.atomic():
            challenge = Challenge.objects.filter(pk=pk, is_published=True).first()
            if not challenge:
                raise NotFound("Challenge not found.")
            today = local_today(request.user)
            existing = ChallengeEnrollment.objects.filter(challenge=challenge, user=request.user).first()
            if existing:
                return Response(challenge_payload(challenge, existing, today))
            if not challenge.start_date <= today <= challenge.end_date:
                raise ValidationError("This challenge is not open for joining today.")
            enrollment, created = ChallengeEnrollment.objects.get_or_create(
                challenge=challenge, user=request.user, defaults={"joined_local_date": today}
            )
            if created:
                reconcile_user(request.user)
            return Response(challenge_payload(challenge, enrollment, today), status=status.HTTP_201_CREATED if created else 200)


class MyChallengesView(APIView):
    def get(self, request):
        enrollments = ChallengeEnrollment.objects.filter(user=request.user).select_related("challenge").order_by("-joined_at")
        total = PointsLedger.objects.filter(user=request.user).aggregate(total=Sum("delta"))["total"] or 0
        badges = BadgeAward.objects.filter(enrollment__user=request.user, is_active=True).select_related("enrollment__challenge")
        today = local_today(request.user)
        return Response({"points_total": total,
                         "challenges": [challenge_payload(e.challenge, e, today) for e in enrollments],
                         "badges": [{"challenge_id": b.enrollment.challenge_id,
                                     "title": b.enrollment.challenge.badge_title} for b in badges]})


class LeaderboardView(APIView):
    def get(self, request):
        scope = request.query_params.get("scope", "individual")
        if scope not in ("individual", "team"):
            raise ValidationError({"scope": "Use individual or team."})
        totals = list(PointsLedger.objects.values("user_id").annotate(points=Sum("delta")).filter(points__gt=0))
        users = {u.id: u for u in type(request.user).objects.filter(id__in=[r["user_id"] for r in totals])}
        profiles = {p.user_id: p for p in EmployeeProfile.objects.select_related("team").filter(user_id__in=users)}
        if scope == "individual":
            ordered = sorted(totals, key=lambda r: (-r["points"], r["user_id"]))[:100]
            return Response([{"rank": i, "name": users[r["user_id"]].full_name if r["user_id"] == request.user.id else "Participant",
                              "points": r["points"], "is_current_user": r["user_id"] == request.user.id}
                             for i, r in enumerate(ordered, 1)])
        teams = {}
        for row in totals:
            profile = profiles.get(row["user_id"])
            if profile and profile.team_id:
                team = teams.setdefault(profile.team_id, {"team_name": profile.team.name, "points": 0, "member_count": 0})
                team["points"] += row["points"]
                team["member_count"] += 1
        own_profile = profiles.get(request.user.id)
        ordered = sorted(teams.items(), key=lambda entry: (-entry[1]["points"], entry[0]))[:100]
        return Response([{"rank": i, **team, "is_user_team": bool(own_profile and own_profile.team_id == team_id)}
                         for i, (team_id, team) in enumerate(ordered, 1)])


class RankingsView(APIView):
    """Live company rankings. GET ?board=steps_today|steps_week|steps_month|points|teams.
    POST {"show_my_name": bool} chooses whether colleagues see your name."""

    def get(self, request):
        from .rankings import BOARDS, board_payload
        board = request.query_params.get("board", "steps_week")
        if board not in BOARDS:
            raise ValidationError({"board": "Use one of: {}.".format(", ".join(BOARDS))})
        data = board_payload(request.user, board, local_today(request.user))
        profile = EmployeeProfile.objects.filter(user=request.user).first()
        data["show_my_name"] = profile.show_in_rankings if profile else True
        return Response(data)

    def post(self, request):
        value = request.data.get("show_my_name")
        if not isinstance(value, bool):
            raise ValidationError({"show_my_name": "Send true or false."})
        profile, _ = EmployeeProfile.objects.get_or_create(user=request.user)
        profile.show_in_rankings = value
        profile.save(update_fields=["show_in_rankings", "updated_at"])
        return Response({"show_my_name": value})
