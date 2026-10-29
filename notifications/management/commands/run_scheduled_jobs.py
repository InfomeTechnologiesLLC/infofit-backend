"""Run every scheduled job once: reminders, coach messages, challenge/program news, then push delivery.

For servers without Celery (for example shared hosting with cPanel), add a cron job that runs every 5 minutes:

    */5 * * * * cd /path/to/infofit-backend && /path/to/python manage.py run_scheduled_jobs >> scheduler.log 2>&1

The portal's Notifications page shows the exact line for your server. Running it more often is safe: every
message is sent at most once.
"""
import os
import tempfile
import time

from django.core.management.base import BaseCommand

from notifications.models import SchedulerHeartbeat

try:
    import fcntl  # Linux servers: stop two cron runs overlapping
except ImportError:  # Windows development machines
    fcntl = None


def _jobs():
    from automations.tasks import run_scheduled_automations
    from challenges.tasks import create_challenge_reminders
    from wellness.tasks import create_weekly_bmi_reminders, create_wellness_reminders
    return (
        ("wellness_reminders", create_wellness_reminders),
        ("bmi_reminders", create_weekly_bmi_reminders),
        ("challenge_reminders", create_challenge_reminders),
        ("automations", run_scheduled_automations),
    )


def run_all(stdout=None):
    """Run the jobs and deliver pushes. Returns {job: result}; one failing job never stops the others."""
    from notifications.tasks import deliver_pending_pushes
    results = {}
    for name, job in _jobs():
        try:
            results[name] = job()
        except Exception as error:  # noqa: BLE001 - keep going, report it
            results[name] = "error: {}".format(error)[:200]
    try:
        results["push_delivery"] = deliver_pending_pushes(rounds=20)
    except Exception as error:  # noqa: BLE001
        results["push_delivery"] = "error: {}".format(error)[:200]
    ok = not any(str(value).startswith("error") for value in results.values())
    SchedulerHeartbeat.beat("cron", ok=ok, summary=results)
    return results


class Command(BaseCommand):
    help = "Run reminders, coach messages, challenge news and push delivery once (for cron)."

    def handle(self, *args, **options):
        lock_path = os.path.join(tempfile.gettempdir(), "infome-fit-scheduled-jobs.lock")
        with open(lock_path, "w") as lock:
            if fcntl is not None:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    self.stdout.write("Another run is still going; skipping this one.")
                    return
            started = time.time()
            results = run_all()
            self.stdout.write("{} {} ({:.1f}s)".format(time.strftime("%Y-%m-%d %H:%M:%S"), results,
                                                       time.time() - started))
