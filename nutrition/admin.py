from django.contrib import admin

from .models import Meal, MealItem


class MealItemInline(admin.TabularInline):
    model = MealItem
    extra = 0
    can_delete = False
    readonly_fields = ("fdc_id", "food_name", "data_type", "grams", "kcal_per_100g", "kcal",
                       "protein_g", "carbohydrate_g", "fat_g")


@admin.register(Meal)
class MealAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "kind", "local_date", "total_kcal", "created_at")
    list_filter = ("kind", "local_date")
    search_fields = ("user__email", "notes")
    readonly_fields = ("user", "client_request_id", "payload_hash", "kind", "occurred_at",
                       "local_date", "time_zone", "notes", "total_kcal", "protein_g",
                       "carbohydrate_g", "fat_g", "created_at")
    inlines = [MealItemInline]

    def has_module_permission(self, request):
        return bool(request.user.is_authenticated and request.user.is_superuser)

    def has_view_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
