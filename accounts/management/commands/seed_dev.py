"""Create DEMO users for local development only. Refuses to run when DEBUG is off."""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from accounts.models import Department, Role, Team, User

DEMO_PASSWORD = "Demo-Passw0rd!2026"  # demo only; never used outside DEBUG


class Command(BaseCommand):
    help = "Seed demo departments/teams/users (development only)."

    def handle(self, *args, **opts):
        if not settings.DEBUG:
            raise CommandError("seed_dev only runs with DEBUG=True. Never seed demo data in production.")
        dept, _ = Department.objects.get_or_create(name="Engineering (DEMO)")
        team, _ = Team.objects.get_or_create(name="Team Falcon (DEMO)", defaults={"department": dept})
        rows = [
            ("admin@demo.infome.test", Role.ADMIN, "Demo", "Admin", "D000"),
            ("manager@demo.infome.test", Role.TEAM_MANAGER, "Demo", "Manager", "D001"),
            ("asha@demo.infome.test", Role.EMPLOYEE, "Asha", "Demo", "D002"),
            ("bilal@demo.infome.test", Role.EMPLOYEE, "Bilal", "Demo", "D003"),
        ]
        for email, role, first, last, emp in rows:
            user, created = User.objects.get_or_create(
                email=email, defaults={"role": role, "first_name": first, "last_name": last, "employee_id": emp,
                                       "is_staff": role == Role.ADMIN},
            )
            if created:
                user.set_password(DEMO_PASSWORD)
                user.save()
            user.profile.team = team
            user.profile.department = dept
            user.profile.save()
        team.manager = User.objects.get(email="manager@demo.infome.test")
        team.save()
        self.stdout.write(self.style.SUCCESS(f"Seeded demo users. Password for all: {DEMO_PASSWORD}"))
