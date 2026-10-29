"""Tools IVA can call (Gemini function calling).

Two kinds:
- Read tools return the signed-in employee's own data. Every query is filtered by `user`; there is no way
  to name another person.
- Action tools never change anything. They validate the arguments and store a pending AssistantProposal
  that the employee confirms (or cancels) in the app. See services.confirm for what each one does.

Arguments come from the model and are treated as untrusted input: anything unexpected is rejected, never repaired.
"""
from datetime import timedelta

from django.db.models import Count, Sum
from django.utils import timezone

from accounts.models import EmployeeProfile
from ai_service.tools import own_challenge_summary, own_fitness_summary
from automations.models import StepGoalSuggestion
from challenges.models import Challenge, ChallengeEnrollment
from fitness.models import DailyActivity, FitnessAccount
from nutrition import foods
from nutrition.models import Meal
from wellness.models import HydrationLog, ProgramCheckIn, ProgramEnrollment, ReminderSettings, WellnessProgram
from wellness.views import employee_time_zone, visible_programs

from .models import AssistantProposal

PROPOSAL_TTL = timedelta(minutes=30)
MEAL_KINDS = ("breakfast", "lunch", "dinner", "snack")
DIETS = ("vegetarian", "vegan", "eggetarian", "non_vegetarian", "jain", "pescatarian", "no_preference")
PREFERENCE_KEYS = {"diet": 40, "cuisine": 60, "dislikes": 120, "activity": 120, "notes": 200}


class ToolError(Exception):
    """A tool call that cannot be done; the message is shown to the model so it can explain or ask."""


# ---- helpers --------------------------------------------------------------------------------------

def _today(user):
    _, zone = employee_time_zone(user)
    return timezone.now().astimezone(zone).date()


def _int(args, key, low, high, required=True):
    value = args.get(key)
    if value is None and not required:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
        raise ToolError(f"{key} must be a whole number.")
    value = int(value)
    if not low <= value <= high:
        raise ToolError(f"{key} must be between {low} and {high}.")
    return value


def _text(args, key, limit, required=True):
    value = args.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not " ".join(value.split()):
        raise ToolError(f"{key} must be text.")
    return " ".join(value.split())[:limit]


def _goal(user):
    return EmployeeProfile.objects.filter(user=user).values_list("daily_step_goal", flat=True).first() or 10000


def preferences(user):
    prefs = EmployeeProfile.objects.filter(user=user).values_list("wellness_preferences", flat=True).first() or {}
    saved = prefs.get("iva") if isinstance(prefs, dict) else None
    return {k: v for k, v in (saved or {}).items() if k in PREFERENCE_KEYS and isinstance(v, str)}


def _propose(user, action, payload, summary, details):
    return AssistantProposal.objects.create(
        user=user, action=action, payload=payload, summary=summary[:240], details=details[:10],
        expires_at=timezone.now() + PROPOSAL_TTL)


def _minute_label(minute):
    return "midnight" if minute >= 1440 else f"{minute // 60:02d}:{minute % 60:02d}"


# ---- read tools ---------------------------------------------------------------------------------------

def get_today_summary(user, args):
    today = _today(user)
    fitness = own_fitness_summary(user)
    goal = _goal(user)
    water = HydrationLog.objects.filter(user=user, local_date=today).aggregate(total=Sum("volume_ml"), entries=Count("id"))
    meals = list(Meal.objects.filter(user=user, local_date=today).values("kind", "total_kcal"))
    result = {"local_date": today.isoformat(), "daily_step_goal": goal, "fitness": fitness,
              "water_logged_ml": water["total"] or 0, "water_entries": water["entries"], "water_is_self_reported": True,
              "meals_saved": [{"kind": m["kind"], "estimated_kcal": round(float(m["total_kcal"]))} for m in meals]}
    if fitness.get("status") == "available" and fitness.get("steps") is not None:
        result["remaining_steps_today"] = max(0, goal - fitness["steps"])
    pending = StepGoalSuggestion.objects.filter(user=user, status=StepGoalSuggestion.Status.PENDING).first()
    if pending:
        result["pending_step_goal_suggestion"] = {"suggested_goal": pending.suggested_goal, "reason": pending.reason}
    return result


def get_step_history(user, args):
    days = _int(args, "days", 1, 31, required=False) or 7
    today = _today(user)
    account = FitnessAccount.objects.filter(user=user).first()
    goal = _goal(user)
    if not account or not account.active_source_id:
        return {"status": "no_active_source", "note": "Steps have not been connected on a phone."}
    rows = DailyActivity.objects.filter(user=user, source_id=account.active_source_id,
                                        local_date__range=(today - timedelta(days=days - 1), today)).order_by("local_date")
    history = [{"date": r.local_date.isoformat(), "steps": r.steps, "goal_met": r.steps >= goal}
               for r in rows if r.metric_status.get("steps") == "available" and r.steps is not None]
    return {"daily_step_goal": goal, "days_requested": days, "days_with_data": len(history), "days": history,
            "note": "Days without synced data are missing, not zero."}


def get_meals(user, args):
    days = _int(args, "days", 1, 14, required=False) or 1
    today = _today(user)
    meals = (Meal.objects.filter(user=user, local_date__gte=today - timedelta(days=days - 1))
             .prefetch_related("items").order_by("-occurred_at")[:30])
    return {"meals": [{"meal_id": m.pk, "date": m.local_date.isoformat(), "kind": m.kind,
                       "estimated_kcal": round(float(m.total_kcal)),
                       "items": [{"food": i.food_name, "grams": float(i.grams)} for i in m.items.all()]}
                      for m in meals],
            "note": "Calories are estimates from food databases and the portions the employee entered."}


def get_hydration(user, args):
    days = _int(args, "days", 1, 14, required=False) or 1
    today = _today(user)
    rows = (HydrationLog.objects.filter(user=user, local_date__gte=today - timedelta(days=days - 1))
            .values("local_date").annotate(total=Sum("volume_ml"), entries=Count("id")).order_by("local_date"))
    return {"days": [{"date": r["local_date"].isoformat(), "logged_ml": r["total"], "entries": r["entries"]} for r in rows],
            "note": "Self-reported. A day without entries does not prove the employee did not drink."}


def get_challenges(user, args):
    today = _today(user)
    joined = set(ChallengeEnrollment.objects.filter(user=user).values_list("challenge_id", flat=True))
    open_now = Challenge.objects.filter(is_published=True, start_date__lte=today, end_date__gte=today).exclude(id__in=joined)
    return {**own_challenge_summary(user),
            "open_to_join": [{"challenge_id": c.pk, "title": c.title, "kind": c.kind, "ends": c.end_date.isoformat(),
                              "goal": c.target_label, "what_counts": c.counting_note,
                              "reward_points": c.reward_points} for c in open_now[:10]]}


def get_wellness_programs(user, args):
    today = _today(user)
    programs = visible_programs(user).filter(start_date__lte=today, end_date__gte=today)[:20]
    enrolled = {e.program_id: e for e in ProgramEnrollment.objects.filter(user=user, program__in=programs)}
    checked = set(ProgramCheckIn.objects.filter(enrollment__in=enrolled.values(), local_date=today)
                  .values_list("enrollment__program_id", flat=True))
    return {"programs": [{"program_id": p.pk, "title": p.title, "personal_plan": p.owner_id is not None,
                          "today_action": p.action_for(today), "joined": p.pk in enrolled,
                          "checked_in_today": p.pk in checked} for p in programs]}


def get_reminder_settings(user, args):
    s = ReminderSettings.objects.filter(user=user).first()
    if not s:
        return {"water": "off", "movement": "off", "challenge": "off"}
    return {"water_enabled": s.water_enabled, "water_every_minutes": s.water_interval_minutes,
            "movement_enabled": s.movement_enabled, "movement_every_minutes": s.movement_interval_minutes,
            "window": f"{_minute_label(s.start_minute)} to {_minute_label(s.end_minute)}",
            "challenge_enabled": s.challenge_enabled, "challenge_time": _minute_label(s.challenge_minute)}


def get_preferences(user, args):
    return {"saved_preferences": preferences(user)}


def search_food(user, args):
    query = _text(args, "query", 80)
    try:
        found = foods.search(query, limit=5)
    except Exception:
        raise ToolError("Food lookup is unavailable right now.")
    return {"results": [{"food": f["description"], "kcal_per_100g": f["kcal_per_100g"],
                         "typical_serving_g": f.get("serving_g"), "serving_unit": f.get("serving_unit"),
                         "source": f["data_type"]} for f in found]}


# ---- action tools (drafts only) --------------------------------------------------------------------------

def log_water(user, args):
    volume = _int(args, "volume_ml", 50, 1500)
    return _propose(user, "log_water", {"volume_ml": volume}, f"Log {volume} ml of water",
                    [f"{volume} ml (an estimate from what you said)"])


def _meal_kind(args, user):
    kind = args.get("meal_kind")
    if kind in MEAL_KINDS:
        return kind
    _, zone = employee_time_zone(user)
    hour = timezone.now().astimezone(zone).hour
    return "breakfast" if 5 <= hour < 11 else "lunch" if 11 <= hour < 16 else "snack" if 16 <= hour < 18 else "dinner"


def meal_items(raw_items):
    """[{food, grams | servings}] -> (resolved items, details, missing). Shared by IVA and Lens."""
    if not isinstance(raw_items, list) or not 1 <= len(raw_items) <= 8:
        raise ToolError("List one to eight foods.")
    items, details, missing, total = [], [], [], 0
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise ToolError("Each item needs a food name.")
        name = _text(raw, "food", 80)
        match = foods.best_match(name)
        if not match:
            missing.append(name)
            continue
        if raw.get("grams") is not None:
            grams = _int(raw, "grams", 5, 1500)
            portion = f"{grams} g"
        elif raw.get("servings") is not None and match.get("serving_g"):
            servings = raw["servings"]
            if isinstance(servings, bool) or not isinstance(servings, (int, float)) or not 0.25 <= servings <= 10:
                raise ToolError("servings must be between 0.25 and 10.")
            grams = int(round(match["serving_g"] * servings))
            unit = match.get("serving_unit") or "serving"
            portion = f"{servings:g} {unit}{'' if servings == 1 else 's'} (about {grams} g)"
        else:
            raise ToolError(f"Estimate grams for {name}.")
        kcal = int(round(match["kcal_per_100g"] * grams / 100))
        total += kcal
        items.append({"food_id": match["food_id"], "description": match["description"], "grams": grams, "kcal": kcal})
        details.append(f"{match['description']}: {portion}, about {kcal} kcal (portion estimated)")
    return items, details, missing, total


def log_meal(user, args):
    kind = _meal_kind(args, user)
    items, details, missing, total = meal_items(args.get("items"))
    if not items:
        raise ToolError("None of those foods were found in the food databases: " + ", ".join(missing))
    if missing:
        details.append("Could not find: " + ", ".join(missing))
    return _propose(user, "log_meal", {"kind": kind, "items": items, "kcal": total},
                    f"Log {kind}: about {total} kcal", details)


def repeat_meal(user, args):
    meal_id = _int(args, "meal_id", 1, 2**62, required=False)
    meals = Meal.objects.filter(user=user).prefetch_related("items").order_by("-occurred_at")
    if meal_id:
        meal = meals.filter(pk=meal_id).first()
    else:
        kind = args.get("meal_kind")
        if kind in MEAL_KINDS:
            meals = meals.filter(kind=kind)
        if args.get("when") == "yesterday":
            meals = meals.filter(local_date=_today(user) - timedelta(days=1))
        meal = meals.first()
    if not meal:
        raise ToolError("No matching saved meal was found.")
    kind = args["as_meal_kind"] if args.get("as_meal_kind") in MEAL_KINDS else meal.kind
    items = list(meal.items.all())
    return _propose(user, "repeat_meal", {"meal_id": meal.pk, "kind": kind},
                    f"Log {kind} again: about {int(meal.total_kcal)} kcal",
                    [f"Same as your {meal.kind} on {meal.local_date:%d %b}"] +
                    [f"{i.food_name}: {float(i.grams):g} g" for i in items[:6]])


def join_challenge(user, args):
    challenge_id = _int(args, "challenge_id", 1, 2**62)
    today = _today(user)
    challenge = Challenge.objects.filter(pk=challenge_id, is_published=True).first()
    if not challenge:
        raise ToolError("That challenge was not found. Call get_challenges for the open ones.")
    if ChallengeEnrollment.objects.filter(user=user, challenge=challenge).exists():
        raise ToolError("The employee has already joined this challenge.")
    if not challenge.start_date <= today <= challenge.end_date:
        raise ToolError("That challenge is not open for joining today.")
    target = challenge.target_label
    return _propose(user, "join_challenge", {"challenge_id": challenge.pk}, f"Join {challenge.title}",
                    [f"Goal: {target} by {challenge.end_date:%d %b}", f"{challenge.reward_points} points on completion"])


def set_reminder(user, args):
    kind = args.get("reminder")
    if kind not in ("water", "movement", "challenge"):
        raise ToolError("reminder must be water, movement or challenge.")
    if not isinstance(args.get("enabled"), bool):
        raise ToolError("enabled must be true or false.")
    change = {f"{kind}_enabled": args["enabled"]}
    details = [f"{kind.title()} reminders {'on' if args['enabled'] else 'off'}"]
    if kind in ("water", "movement") and args.get("interval_minutes") is not None:
        change[f"{kind}_interval_minutes"] = _int(args, "interval_minutes", 15, 360)
        details.append(f"Every {change[f'{kind}_interval_minutes']} minutes")
    if args.get("start_minute") is not None or args.get("end_minute") is not None:
        current = ReminderSettings.objects.filter(user=user).first()
        start = _int(args, "start_minute", 0, 1439, required=False)
        end = _int(args, "end_minute", 1, 1440, required=False)
        start = start if start is not None else (current.start_minute if current else 540)
        end = end if end is not None else (current.end_minute if current else 1140)
        if start >= end:
            raise ToolError("The reminder window must end after it starts.")
        change.update({"start_minute": start, "end_minute": end})
        details.append(f"Between {_minute_label(start)} and {_minute_label(end)}")
    if kind == "challenge" and args.get("minute_of_day") is not None:
        change["challenge_minute"] = _int(args, "minute_of_day", 0, 1439)
        details.append(f"Daily at {_minute_label(change['challenge_minute'])}")
    verb = "Turn on" if args["enabled"] else "Turn off"
    return _propose(user, "set_reminder", {"change": change}, f"{verb} {kind} reminders", details)


def set_step_goal(user, args):
    goal = _int(args, "daily_step_goal", 2000, 30000)
    current = _goal(user)
    if goal == current:
        raise ToolError(f"The daily step goal is already {goal}.")
    return _propose(user, "set_step_goal", {"daily_step_goal": goal}, f"Change your daily step goal to {goal:,}",
                    [f"From {current:,} to {goal:,} steps a day"])


def check_in_program(user, args):
    program_id = _int(args, "program_id", 1, 2**62)
    today = _today(user)
    program = visible_programs(user).filter(pk=program_id, start_date__lte=today, end_date__gte=today).first()
    enrollment = ProgramEnrollment.objects.filter(user=user, program=program).first() if program else None
    if not enrollment:
        raise ToolError("That program is not one the employee has joined. Call get_wellness_programs.")
    if ProgramCheckIn.objects.filter(enrollment=enrollment, local_date=today).exists():
        raise ToolError("Already checked in today.")
    return _propose(user, "program_checkin", {"program_id": program.pk}, f"Check in to {program.title}",
                    [f"Today's action: {program.action_for(today)}"])


def create_weekly_plan(user, args):
    title = _text(args, "title", 80)
    days = args.get("days")
    if not isinstance(days, list) or len(days) != 7 or not all(isinstance(d, str) and d.strip() for d in days):
        raise ToolError("days must be exactly seven short daily actions.")
    clean = [" ".join(d.split())[:160] for d in days]
    start = _today(user)
    if WellnessProgram.objects.filter(owner=user, end_date__gte=start).count() >= 3:
        raise ToolError("The employee already has three active personal plans.")
    return _propose(user, "start_plan", {"title": title, "days": clean, "start": start.isoformat()},
                    f"Start your plan: {title}",
                    [f"Day {i + 1}: {d}" for i, d in enumerate(clean)])


def remember_preference(user, args):
    key = args.get("key")
    if key not in PREFERENCE_KEYS:
        raise ToolError("key must be one of: " + ", ".join(PREFERENCE_KEYS))
    value = _text(args, "value", PREFERENCE_KEYS[key])
    if key == "diet":
        value = value.lower().replace("-", "_").replace(" ", "_")
        if value not in DIETS:
            raise ToolError("diet must be one of: " + ", ".join(DIETS))
    label = key.replace("_", " ")
    return _propose(user, "save_preference", {"key": key, "value": value},
                    f"Remember your {label}: {value.replace('_', ' ')}",
                    ["IVA will use this when suggesting meals and plans. You can change it in Settings."])


# ---- declarations -------------------------------------------------------------------------------------------

def _obj(properties=None, required=None):
    schema = {"type": "object", "properties": properties or {}}
    if required:
        schema["required"] = required
    return schema


INT = {"type": "integer"}
STR = {"type": "string"}

READ_TOOLS = {
    "get_today_summary": (get_today_summary, "Today's synced steps, step goal, remaining steps, water logged and meals saved.", _obj()),
    "get_step_history": (get_step_history, "Daily synced steps for the last N days (1-31) and whether the goal was met.",
                         _obj({"days": INT})),
    "get_meals": (get_meals, "Meals the employee saved in the last N days (1-14), with items and estimated calories.",
                  _obj({"days": INT})),
    "get_hydration": (get_hydration, "Self-reported water per day for the last N days (1-14).", _obj({"days": INT})),
    "get_challenges": (get_challenges, "Points, joined challenges with progress, and challenges open to join (with ids).", _obj()),
    "get_wellness_programs": (get_wellness_programs, "Wellness programs and personal plans with today's action and check-in status.", _obj()),
    "get_reminder_settings": (get_reminder_settings, "Current water, movement and challenge reminder settings.", _obj()),
    "get_preferences": (get_preferences, "Saved food and activity preferences (diet, cuisine, dislikes, activity, notes).", _obj()),
    "search_food": (search_food, "Look up a food's calories per 100 g and typical serving in Indian and USDA food databases.",
                    _obj({"query": STR}, ["query"])),
}

ACTION_TOOLS = {
    "log_water": (log_water, "Draft a water log after the employee says they drank water. 250 ml for a glass, 500 for a bottle.",
                  _obj({"volume_ml": INT}, ["volume_ml"])),
    "log_meal": (log_meal, "Draft a meal log after the employee says they ate food. Give grams, or servings for Indian dishes "
                           "like idli, chapati or dosa. Never add foods they did not mention.",
                 _obj({"meal_kind": {"type": "string", "enum": list(MEAL_KINDS)},
                       "items": {"type": "array", "items": _obj({"food": STR, "grams": INT, "servings": {"type": "number"}}, ["food"])}},
                      ["items"])),
    "repeat_meal": (repeat_meal, "Draft logging a previously saved meal again, e.g. 'same breakfast as yesterday'.",
                    _obj({"meal_id": INT, "meal_kind": {"type": "string", "enum": list(MEAL_KINDS)},
                          "when": {"type": "string", "enum": ["yesterday", "last"]},
                          "as_meal_kind": {"type": "string", "enum": list(MEAL_KINDS)}})),
    "join_challenge": (join_challenge, "Draft joining an open challenge by id (from get_challenges).",
                       _obj({"challenge_id": INT}, ["challenge_id"])),
    "set_reminder": (set_reminder, "Draft turning a reminder on or off or changing its timing. Times are minutes after midnight.",
                     _obj({"reminder": {"type": "string", "enum": ["water", "movement", "challenge"]},
                           "enabled": {"type": "boolean"}, "interval_minutes": INT,
                           "start_minute": INT, "end_minute": INT, "minute_of_day": INT}, ["reminder", "enabled"])),
    "set_step_goal": (set_step_goal, "Draft changing the daily step goal (2,000-30,000).",
                      _obj({"daily_step_goal": INT}, ["daily_step_goal"])),
    "check_in_program": (check_in_program, "Draft today's check-in for a joined wellness program or personal plan.",
                         _obj({"program_id": INT}, ["program_id"])),
    "create_weekly_plan": (create_weekly_plan, "Draft a personal 7-day walking or workout plan: a short title and exactly "
                                               "seven gentle daily actions that fit the employee's recent activity.",
                           _obj({"title": STR, "days": {"type": "array", "items": STR}}, ["title", "days"])),
    "remember_preference": (remember_preference, "Draft saving a preference the employee stated, e.g. that they are vegetarian.",
                            _obj({"key": {"type": "string", "enum": list(PREFERENCE_KEYS)}, "value": STR}, ["key", "value"])),
}


def declarations():
    # Gemini rejects an OBJECT schema with no properties, so tools without arguments omit `parameters`.
    out = []
    for name, (_, description, schema) in {**READ_TOOLS, **ACTION_TOOLS}.items():
        entry = {"name": name, "description": description}
        if schema["properties"]:
            entry["parameters"] = schema
        out.append(entry)
    return out
