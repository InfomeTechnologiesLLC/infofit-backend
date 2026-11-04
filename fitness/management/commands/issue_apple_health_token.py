from django.core.management.base import BaseCommand, CommandError
from rest_framework.exceptions import APIException

from accounts.models import User
from fitness.apple_health import issue_token


class Command(BaseCommand):
    help = "Issue/rotate a HealthySync upload token for an employee. The token is displayed only once."

    def add_arguments(self, parser):
        parser.add_argument("email")
        parser.add_argument("--replace-existing", action="store_true")
        parser.add_argument("--new-device", action="store_true")

    def handle(self, *args, **options):
        user = User.objects.filter(email__iexact=options["email"], is_active=True).first()
        if user is None:
            raise CommandError("No active employee with that email.")
        try:
            _, token = issue_token(user, replace_existing=options["replace_existing"], new_device=options["new_device"])
        except APIException as exc:
            raise CommandError(str(exc.detail))
        self.stdout.write(token)
