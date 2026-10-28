from django.db import migrations


def load(apps, schema_editor):
    from nutrition.foods import load_food_csv
    load_food_csv(apps.get_model("nutrition", "FoodItem"))


class Migration(migrations.Migration):
    """Loads the bundled INDB + IFCT 2017 food data (about 1,550 rows). Re-run any time with load_indian_foods."""
    dependencies = [("nutrition", "0002_mealitem_food_code_mealitem_food_source_and_more")]
    operations = [migrations.RunPython(load, migrations.RunPython.noop)]
