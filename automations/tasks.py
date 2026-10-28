import logging

from celery import shared_task

from . import services

log = logging.getLogger("infome.automations")


@shared_task
def run_scheduled_automations():
    """One beat entry (every 5 minutes) drives every automation; a failure in one never blocks the others."""
    results = {}
    for name, func in (
        ("daily_coach", services.send_daily_coach),
        ("weekly_review", services.send_weekly_reviews),
        ("step_goal_suggestions", services.create_step_goal_suggestions),
        ("reengagement", services.send_reengagement),
        ("hr_digest", services.send_hr_digests),
        ("goal_nudges", services.send_goal_nudges),
        ("challenge_alerts", services.send_challenge_alerts),
        ("meal_prompts", services.send_meal_prompts),
    ):
        try:
            results[name] = func()
        except Exception:
            log.exception("Automation %s failed", name)
            results[name] = "error"
    return results
