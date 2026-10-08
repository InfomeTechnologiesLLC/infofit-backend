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


class MealItem(models.Model):
    meal = models.ForeignKey(Meal, on_delete=models.CASCADE, related_name="items")
    fdc_id = models.PositiveBigIntegerField()
    food_name = models.CharField(max_length=255)
    data_type = models.CharField(max_length=40)
    grams = models.DecimalField(max_digits=8, decimal_places=1)
    kcal_per_100g = models.DecimalField(max_digits=7, decimal_places=2)
    kcal = models.DecimalField(max_digits=9, decimal_places=2)
    protein_g = models.DecimalField(max_digits=9, decimal_places=2, null=True, blank=True)
    carbohydrate_g = models.DecimalField(max_digits=9, decimal_places=2, null=True, blank=True)
    fat_g = models.DecimalField(max_digits=9, decimal_places=2, null=True, blank=True)
