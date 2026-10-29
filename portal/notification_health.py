"""Plain-language checks for the portal's Notifications page: is everything needed to reach phones in place?"""
import os
import sys
from datetime import timedelta

from django.conf import settings
from django.db.models import Count, Q
from django.utils import timezone

from accounts.models import Role, User
from automations.models import AutomationSettings
from notifications.models import Notification, PushDelivery, PushDevice, SchedulerHeartbeat
from notifications.tasks import push_problem

STALE_AFTER = timedelta(minutes=15)


def _python():
    """The virtualenv's own python (the web server may run Django through a different launcher)."""
    candidate = os.path.join(sys.prefix, "bin", "python")
    return candidate if os.path.exists(candidate) else sys.executable


def cron_line():
    base = str(settings.BASE_DIR)
    # The log goes to the home folder, never inside the website folder where it could be downloaded.
    log = os.path.join(os.path.expanduser("~"), "infome-fit-scheduler.log")
    return "*/5 * * * * cd {} && {} manage.py run_scheduled_jobs >> {} 2>&1".format(base, _python(), log)


def scheduler_status(now=None):
    now = now or timezone.now()
    beats = {b.name: b for b in SchedulerHeartbeat.objects.all()}
    runs = [b for name, b in beats.items() if name in ("cron", "automations")]
    last = max(runs, key=lambda b: b.last_run_at) if runs else None
    ok = bool(last and now - last.last_run_at <= STALE_AFTER)
    return {"ok": ok, "last": last, "never": last is None, "beats": beats}


def checks(now=None):
    """[{title, ok, detail, fix}] in the order someone should fix them."""
    now = now or timezone.now()
    employees = User.objects.filter(is_active=True, role__in=[Role.EMPLOYEE, Role.TEAM_MANAGER])
    total = employees.count()
    with_phone = employees.filter(push_devices__enabled=True).distinct().count()
    sched = scheduler_status(now)
    problem = push_problem()
    coach_on = AutomationSettings.objects.filter(user__in=employees, daily_coach_enabled=True).count()
    rows = []
    if sched["ok"]:
        detail = "Last ran {} ago.".format(_ago(now - sched["last"].last_run_at))
    elif sched["never"]:
        detail = "It has never run on this server, so coach messages, challenge news and pushes are not going out."
    else:
        detail = "Last ran {} ago, so messages are waiting.".format(_ago(now - sched["last"].last_run_at))
    rows.append({"title": "Scheduler", "ok": sched["ok"], "detail": detail,
                 "fix": None if sched["ok"] else "cron"})
    rows.append({"title": "Push service (Firebase)", "ok": not problem,
                 "detail": "Set up. Phones can receive notifications." if not problem else problem,
                 "fix": None if not problem else
                 "In the Firebase console, open Project settings > Service accounts > Generate new private key. "
                 "Upload the JSON file to the server (outside public_html) and set FIREBASE_CREDENTIALS_FILE to its "
                 "full path in the .env file, then restart the site."})
    rows.append({"title": "Phones registered", "ok": with_phone > 0,
                 "detail": "{} of {} employees have the app set up to receive notifications.".format(with_phone, total),
                 "fix": None if with_phone else
                 "The app registers the phone when someone signs in. If this stays at 0, the app was built without "
                 "android/app/google-services.json (download it from the same Firebase project), or people "
                 "did not allow notifications."})
    rows.append({"title": "Coach messages", "ok": coach_on > 0,
                 "detail": "Turned on for {} of {} employees. Each person can turn them off in the app's Settings.".format(
                     coach_on, total), "fix": None})
    has_ai = bool(getattr(settings, "GEMINI_API_KEY", ""))
    rows.append({"title": "AI wording for coach messages", "ok": has_ai,
                 "detail": "Gemini writes each message from the person's own data." if has_ai else
                 "No GEMINI_API_KEY, so coach messages use fixed sentences and can feel repetitive.",
                 "fix": None if has_ai else "Add GEMINI_API_KEY to the server .env file and restart the site."})
    return rows


def delivery_counts(now=None):
    now = now or timezone.now()
    recent = PushDelivery.objects.filter(notification__created_at__gte=now - timedelta(hours=24))
    counts = recent.aggregate(
        sent=Count("pk", filter=Q(state=PushDelivery.State.SENT)),
        waiting=Count("pk", filter=Q(state__in=[PushDelivery.State.PENDING, PushDelivery.State.RETRY])),
        failed=Count("pk", filter=Q(state=PushDelivery.State.INVALID)))
    counts["inbox"] = Notification.objects.filter(created_at__gte=now - timedelta(hours=24)).count()
    return counts


def recent_notifications(limit=30):
    rows = list(Notification.objects.select_related("user").prefetch_related("deliveries")
                .exclude(kind=Notification.Kind.HR_DIGEST).order_by("-created_at")[:limit])
    for note in rows:
        states = [d.state for d in note.deliveries.all()]
        if not states:
            note.push_state, note.push_tone = "In app only", "secondary"
        elif PushDelivery.State.SENT in states:
            note.push_state, note.push_tone = "Sent to phone", "success"
        elif any(s in (PushDelivery.State.PENDING, PushDelivery.State.RETRY) for s in states):
            note.push_state, note.push_tone = "Waiting", "warning"
        else:
            note.push_state, note.push_tone = "Not delivered", "danger"
    return rows


def has_problem():
    """Cheap check for the sidebar warning dot."""
    return bool(push_problem()) or not scheduler_status()["ok"]


def _ago(delta):
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "less than a minute"
    if minutes < 60:
        return "{} minute{}".format(minutes, "" if minutes == 1 else "s")
    hours = minutes // 60
    if hours < 48:
        return "{} hour{}".format(hours, "" if hours == 1 else "s")
    return "{} days".format(hours // 24)
