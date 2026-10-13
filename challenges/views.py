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
        "counting_note": ("Only recorded running sessions starting after you join count."
                          if challenge.kind == Challenge.Kind.RUNNING else
                          "Daily steps start counting on the next local day after you join."),
        "complete": progress.is_complete if progress else False,
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
