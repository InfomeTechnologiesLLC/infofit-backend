"""Read-only facts for IVA; every query is restricted to the authenticated user."""
import re
from backports.zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db.models import Count, Q, Sum
from django.utils import timezone

from accounts.models import EmployeeProfile
from challenges.models import ChallengeEnrollment, PointsLedger
from fitness.models import DailyActivity, FitnessAccount
from nutrition.models import Meal
from wellness.models import HydrationLog, ReminderSettings
from .coaching import weekly_facts


FITNESS_TERMS = re.compile(r"\b(steps?|walk(?:ed|ing)?|run(?:ning)?|workouts?|distance|calories|exercise|sleep|activity|fitness)\b")
CHALLENGE_TERMS = re.compile(r"\b(challenges?|points?|badges?|progress)\b")
NUTRITION_TERMS = re.compile(r"\b(meals?|food|eat|ate|eaten|eating|diet|nutrition|intake|protein|carbs?|fat|lunch|breakfast|dinner)\b")
HYDRATION_TERMS = re.compile(r"\b(water|hydration|hydrate|drink|drank)\b")
PERSONAL_TERMS = re.compile(r"\b(my|me|i|mine|today|yesterday)\b")
WEEK_TERMS = re.compile(r"\b(this week|last week|weekly|past seven days|past 7 days)\b")
STEP_TERMS = re.compile(r"\b(steps?|step goal)\b")


def wants_personal_context(message):
    query = message.lower()
    return bool(PERSONAL_TERMS.search(query) and
                (FITNESS_TERMS.search(query) or CHALLENGE_TERMS.search(query) or
                 NUTRITION_TERMS.search(query) or HYDRATION_TERMS.search(query) or WEEK_TERMS.search(query)))


def own_hydration_summary(user):
    settings = ReminderSettings.objects.filter(user=user).first()
    account = FitnessAccount.objects.filter(user=user).first()
    zone_name = settings.time_zone if settings else (account.time_zone if account else "UTC")
    try:
        local_today = timezone.localtime(timezone.now(), ZoneInfo(zone_name)).date()
    except ZoneInfoNotFoundError:
        local_today = timezone.now().date()
    values = HydrationLog.objects.filter(user=user, local_date=local_today).aggregate(
        count=Count("id"), total_ml=Sum("volume_ml"))
    if not values["count"]:
        return {"status": "no_logged_water", "local_date": local_today.isoformat()}
    return {"status": "available", "local_date": local_today.isoformat(),
            "logged_ml": values["total_ml"], "entries": values["count"], "self_reported": True}


def own_nutrition_summary(user):
    account = FitnessAccount.objects.filter(user=user).first()
    latest = Meal.objects.filter(user=user).order_by("-occurred_at").first()
    zone_name = latest.time_zone if latest else (account.time_zone if account else "UTC")
    try:
        local_today = timezone.localtime(timezone.now(), ZoneInfo(zone_name)).date()
    except ZoneInfoNotFoundError:
        local_today = timezone.now().date()
    meals = Meal.objects.filter(user=user, local_date=local_today)
    values = meals.aggregate(count=Count("id"), kcal=Sum("total_kcal"),
                             protein=Sum("protein_g"), carbs=Sum("carbohydrate_g"), fat=Sum("fat_g"),
                             missing_protein=Count("id", filter=Q(protein_g__isnull=True)),
                             missing_carbs=Count("id", filter=Q(carbohydrate_g__isnull=True)),
                             missing_fat=Count("id", filter=Q(fat_g__isnull=True)))
    if not values["count"]:
        return {"status": "no_data", "local_date": local_today.isoformat()}
    return {"status": "available", "local_date": local_today.isoformat(), "meal_count": values["count"],
            "estimated_food_kcal": float(values["kcal"]),
            "protein_g": float(values["protein"]) if not values["missing_protein"] else None,
            "carbohydrate_g": float(values["carbs"]) if not values["missing_carbs"] else None,
            "fat_g": float(values["fat"]) if not values["missing_fat"] else None}


def own_fitness_summary(user):
    account = FitnessAccount.objects.filter(user=user).select_related("active_source").first()
    zone_name = account.time_zone if account else "UTC"
    try:
        local_today = timezone.localtime(timezone.now(), ZoneInfo(zone_name)).date()
    except ZoneInfoNotFoundError:
        local_today = timezone.now().date()
    day = DailyActivity.objects.filter(user=user, local_date=local_today).first()
    if not day or not account or day.source_id != account.active_source_id:
        return {"status": "no_current_source_data", "local_date": None}
    return {
        "status": "available", "local_date": day.local_date.isoformat(), "time_zone": day.time_zone,
        "steps": day.steps, "distance_m": day.distance_m, "active_kcal": day.active_kcal,
        "exercise_minutes": day.exercise_minutes, "sleep_minutes": day.sleep_minutes,
        "metric_status": day.metric_status,
    }


def own_challenge_summary(user):
    enrollments = ChallengeEnrollment.objects.filter(user=user).select_related("challenge", "progress")[:10]
    points = PointsLedger.objects.filter(user=user).aggregate(total=Sum("delta"))["total"] or 0
    return {
        "points": points,
        "challenges": [
            {"title": row.challenge.title, "kind": row.challenge.kind,
             "verified_steps": getattr(row.progress, "verified_steps", None),
             "target_steps": row.challenge.target_steps,
             "verified_running_minutes": getattr(row.progress, "verified_running_minutes", None),
             "target_running_minutes": row.challenge.target_running_minutes,
             "complete": getattr(row.progress, "is_complete", False)}
            for row in enrollments
        ],
    }


def read_context(user, message):
    query = message.lower()
    context = {}
    if FITNESS_TERMS.search(query) or NUTRITION_TERMS.search(query) or CHALLENGE_TERMS.search(query):
        profile = EmployeeProfile.objects.filter(user=user).values("fitness_goal", "daily_step_goal").first()
        if profile:
            context["goal"] = {"fitness_goal": profile["fitness_goal"]}
            if STEP_TERMS.search(query):
                context["goal"]["daily_step_goal"] = profile["daily_step_goal"]
    if not wants_personal_context(message):
        return context, list(context)
    if FITNESS_TERMS.search(query):
        context["fitness"] = own_fitness_summary(user)
        if STEP_TERMS.search(query) and "goal" in context:
            fitness = context["fitness"]
            if fitness["status"] == "available" and fitness["metric_status"].get("steps") == "available" and fitness["steps"] is not None:
                context["goal"]["remaining_steps_today"] = max(0, context["goal"]["daily_step_goal"] - fitness["steps"])
    if NUTRITION_TERMS.search(query):
        context["nutrition"] = own_nutrition_summary(user)
    if CHALLENGE_TERMS.search(query):
        context["challenges"] = own_challenge_summary(user)
    if HYDRATION_TERMS.search(query):
        context["hydration"] = own_hydration_summary(user)
    if WEEK_TERMS.search(query):
        context["weekly"] = weekly_facts(user)
    return context, list(context)
