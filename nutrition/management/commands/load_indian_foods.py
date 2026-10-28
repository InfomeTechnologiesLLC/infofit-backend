from django.core.management.base import BaseCommand

from nutrition.foods import DATA_FILE, load_food_csv


class Command(BaseCommand):
    help = "Load or refresh the Indian food database (INDB recipes and IFCT 2017 ingredients) from the bundled CSV."

    def add_arguments(self, parser):
        parser.add_argument("--file", default=str(DATA_FILE), help="CSV path (defaults to the bundled file).")

    def handle(self, *args, **options):
        count = load_food_csv(path=options["file"])
        self.stdout.write(self.style.SUCCESS(f"Loaded {count} foods."))
