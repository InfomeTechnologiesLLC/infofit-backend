from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.models import User
from challenges.services import reconcile_user
from fitness.apple_health import projection_dates
from fitness.apple_health_normalization import normalize_days
from fitness.models import AppleHealthRecord, FitnessAccount


class Command(BaseCommand):
    help = "Rebuild saved Apple Health daily totals and challenge credit for active Apple Health sources."

    def add_arguments(self, parser):
        parser.add_argument("email", nargs="?", help="Omit to rebuild every active Apple Health source.")

    def handle(self, *args, **options):
        users = User.objects.filter(fitness_account__active_source__kind="apple_health")
        if options["email"]:
            users = users.filter(email__iexact=options["email"])
            if not users.exists():
                raise CommandError("No employee with that email has an active Apple Health source.")
        rebuilt = days = 0
        for user_id in users.order_by("pk").values_list("pk", flat=True).iterator():
            # Match the upload lock order, then recheck the source in case it switched.
            with transaction.atomic():
                user = User.objects.select_for_update().get(pk=user_id)
                account = FitnessAccount.objects.select_for_update().get(user=user)
                source = account.active_source
                if source is None or source.kind != "apple_health":
                    continue
                records = AppleHealthRecord.objects.filter(source=source, first_date__isnull=False)
                dates = set()
                latest = None
                for record in records.iterator():
                    projected = projection_dates(record)
                    if projected:
                        dates.update(projected)
                        if latest is None or record.generated_at > latest.generated_at:
                            latest = record
                if latest is None:
                    continue
                normalize_days(source, dates, latest.generated_at, account.time_zone)
                reconcile_user(user)
                rebuilt += 1
                days += len(dates)
        self.stdout.write(self.style.SUCCESS(f"Rebuilt {days} day(s) for {rebuilt} Apple Health account(s)."))
