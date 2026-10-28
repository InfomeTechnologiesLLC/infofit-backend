from django.conf import settings
from django.db import models


class Meal(models.Model):
    class Kind(models.TextChoices):
        BREAKFAST = "breakfast", "Breakfast"
        LUNCH = "lunch", "Lunch"
        DINNER = "dinner", "Dinner"
        SNACK = "snack", "Snack"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="meals")
    client_request_id = models.UUIDField()
    payload_hash = models.CharField(max_length=64)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    occurred_at = models.DateTimeField()
    local_date = models.DateField(db_index=True)
    time_zone = models.CharField(max_length=64)
    notes = models.CharField(max_length=500, blank=True)
    total_kcal = models.DecimalField(max_digits=9, decimal_places=2)
    protein_g = models.DecimalField(max_digits=9, decimal_places=2, null=True, blank=True)
    carbohydrate_g = models.DecimalField(max_digits=9, decimal_places=2, null=True, blank=True)
    fat_g = models.DecimalField(max_digits=9, decimal_places=2, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "client_request_id"], name="uniq_meal_request_user")]
        indexes = [models.Index(fields=["user", "-occurred_at"], name="nutrition_user_recent_idx")]


class FoodItem(models.Model):
    """Locally stored Indian food composition data (INDB recipes and IFCT 2017 ingredients), per 100 g.

    Loaded from nutrition/data/indian_foods.csv by a migration and by `manage.py load_indian_foods`.
    """
    class Source(models.TextChoices):
        INDB = "indb", "Indian Nutrient Databank"
        IFCT = "ifct", "Indian Food Composition Tables 2017"

    source = models.CharField(max_length=8, choices=Source.choices)
    code = models.CharField(max_length=20)
    name = models.CharField(max_length=255)
    local_names = models.CharField(max_length=500, blank=True)
    search_text = models.CharField(max_length=800, db_index=False)
    kcal_per_100g = models.DecimalField(max_digits=7, decimal_places=2)
    protein_per_100g = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True)
    carbohydrate_per_100g = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True)
    fat_per_100g = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True)
    serving_g = models.PositiveSmallIntegerField(null=True, blank=True)
    serving_unit = models.CharField(max_length=40, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["source", "code"], name="uniq_food_item_source_code")]
        ordering = ["name"]

    @property
    def food_id(self):
        return f"{self.source}:{self.code}"

    def __str__(self):
        return self.name


class MealItem(models.Model):
    meal = models.ForeignKey(Meal, on_delete=models.CASCADE, related_name="items")
    # USDA FoodData Central id; null when the food came from the local Indian database.
    fdc_id = models.PositiveBigIntegerField(null=True, blank=True)
    food_source = models.CharField(max_length=8, default="usda")
    food_code = models.CharField(max_length=20, blank=True)
    food_name = models.CharField(max_length=255)
    data_type = models.CharField(max_length=40)
    grams = models.DecimalField(max_digits=8, decimal_places=1)
    kcal_per_100g = models.DecimalField(max_digits=7, decimal_places=2)
    kcal = models.DecimalField(max_digits=9, decimal_places=2)
    protein_g = models.DecimalField(max_digits=9, decimal_places=2, null=True, blank=True)
    carbohydrate_g = models.DecimalField(max_digits=9, decimal_places=2, null=True, blank=True)
    fat_g = models.DecimalField(max_digits=9, decimal_places=2, null=True, blank=True)
