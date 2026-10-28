import hashlib
import json
from decimal import Decimal, ROUND_HALF_UP
try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python 3.8 hosts
    from backports.zoneinfo import ZoneInfo

from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from . import foods
from .models import Meal, MealItem
from .serializers import MealInputSerializer


class MealConflict(APIException):
    status_code = 409
    default_detail = "This meal request ID was already used for different content."
    default_code = "meal_conflict"


def _two(value):
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _meal_json(meal):
    return {
        "id": meal.id,
        "kind": meal.kind,
        "occurred_at": meal.occurred_at.isoformat(),
        "local_date": meal.local_date.isoformat(),
        "time_zone": meal.time_zone,
        "notes": meal.notes,
        "total_kcal": float(meal.total_kcal),
        "protein_g": float(meal.protein_g) if meal.protein_g is not None else None,
        "carbohydrate_g": float(meal.carbohydrate_g) if meal.carbohydrate_g is not None else None,
        "fat_g": float(meal.fat_g) if meal.fat_g is not None else None,
        "items": [{
            "fdc_id": item.fdc_id, "food_id": item_food_id(item),
            "food_name": item.food_name, "data_type": item.data_type,
            "grams": float(item.grams), "kcal": float(item.kcal),
            "protein_g": float(item.protein_g) if item.protein_g is not None else None,
            "carbohydrate_g": float(item.carbohydrate_g) if item.carbohydrate_g is not None else None,
            "fat_g": float(item.fat_g) if item.fat_g is not None else None,
        } for item in meal.items.all()],
    }


def item_food_id(item):
    return f"usda:{item.fdc_id}" if item.food_source == "usda" else f"{item.food_source}:{item.food_code}"


def _hash_item(item):
    # Items sent with fdc_id keep the original hash shape, so replays from older app versions still match.
    if "food_id" in item:
        return {"food_id": item["food_id"], "grams": str(item["grams"])}
    return {"fdc_id": item["fdc_id"], "grams": str(item["grams"])}


def create_meal(user, data):
    """Create a meal from validated input and return (meal, created).

    Shared by the meals API and by confirmed assistant proposals so both pass the same checks.
    A replay with the same request id returns the existing meal; a changed replay raises MealConflict.
    """
    payload = {
        "kind": data["kind"], "occurred_at": data["occurred_at"].isoformat(),
        "time_zone": data["time_zone"], "notes": data.get("notes", ""),
        "items": [_hash_item(item) for item in data["items"]],
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    existing = Meal.objects.filter(user=user, client_request_id=data["client_request_id"]).prefetch_related("items").first()
    if existing:
        if existing.payload_hash != digest:
            raise MealConflict()
        return existing, False

    resolved = []
    for item in data["items"]:
        food = foods.resolve(food_id=item.get("food_id"), fdc_id=item.get("fdc_id"))
        grams = item["grams"]
        factor = grams / Decimal(100)
        resolved.append({
            "fdc_id": food["fdc_id"], "food_source": food["source"],
            "food_code": "" if food["source"] == "usda" else food["code"],
            "food_name": food["description"],
            "data_type": food["data_type"], "grams": grams,
            "kcal_per_100g": _two(food["kcal_per_100g"]),
            "kcal": _two(Decimal(str(food["kcal_per_100g"])) * factor),
            "protein_g": _two(Decimal(str(food["protein_per_100g"])) * factor) if food["protein_per_100g"] is not None else None,
            "carbohydrate_g": _two(Decimal(str(food["carbohydrate_per_100g"])) * factor) if food["carbohydrate_per_100g"] is not None else None,
            "fat_g": _two(Decimal(str(food["fat_per_100g"])) * factor) if food["fat_per_100g"] is not None else None,
        })
    totals = {}
    for name in ("protein_g", "carbohydrate_g", "fat_g"):
        totals[name] = sum((item[name] for item in resolved), Decimal(0)) if all(
            item[name] is not None for item in resolved) else None
    try:
        with transaction.atomic():
            meal = Meal.objects.create(
                user=user, client_request_id=data["client_request_id"], payload_hash=digest,
                kind=data["kind"], occurred_at=data["occurred_at"],
                local_date=data["occurred_at"].astimezone(ZoneInfo(data["time_zone"])).date(),
                time_zone=data["time_zone"], notes=data.get("notes", ""),
                total_kcal=sum((item["kcal"] for item in resolved), Decimal(0)), **totals,
            )
            MealItem.objects.bulk_create([MealItem(meal=meal, **item) for item in resolved])
    except IntegrityError:
        existing = Meal.objects.filter(user=user, client_request_id=data["client_request_id"]).prefetch_related("items").first()
        if existing is None or existing.payload_hash != digest:
            raise MealConflict()
        return existing, False
    return Meal.objects.prefetch_related("items").get(pk=meal.pk), True


class MealListCreateView(APIView):
    def get(self, request):
        meals = Meal.objects.filter(user=request.user).prefetch_related("items").order_by("-occurred_at", "-id")
        page = PageNumberPagination()
        page.page_size = 20
        selected = page.paginate_queryset(meals, request, view=self)
        return page.get_paginated_response([_meal_json(meal) for meal in selected])

    def post(self, request):
        serializer = MealInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        meal, created = create_meal(request.user, data)
        return Response(_meal_json(meal), status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class MealDetailView(APIView):
    def get(self, request, meal_id):
        meal = get_object_or_404(Meal.objects.prefetch_related("items"), pk=meal_id, user=request.user)
        return Response(_meal_json(meal))

    def delete(self, request, meal_id):
        meal = get_object_or_404(Meal, pk=meal_id, user=request.user)
        meal.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class FrequentMealsView(APIView):
    """Meals the employee saves most often, for one-tap "same as usual" logging."""
    def get(self, request):
        groups = {}
        for meal in Meal.objects.filter(user=request.user).prefetch_related("items").order_by("-occurred_at")[:120]:
            key = (meal.kind, tuple(sorted(item.food_name for item in meal.items.all())))
            if not key[1]:
                continue
            entry = groups.setdefault(key, {"meal": meal, "count": 0})
            entry["count"] += 1
        ranked = sorted(groups.values(), key=lambda g: (-g["count"], -g["meal"].occurred_at.timestamp()))
        return Response({"results": [{**_meal_json(g["meal"]), "times_logged": g["count"]}
                                     for g in ranked if g["count"] >= 2][:6]})


class RepeatMealView(APIView):
    """Save a copy of an earlier meal now. Calories are looked up again, never copied from the app."""
    def post(self, request, meal_id):
        source = get_object_or_404(Meal.objects.prefetch_related("items"), pk=meal_id, user=request.user)
        # A retried request (same id) returns the copy already made, even though "now" has moved on.
        import uuid as _uuid
        try:
            request_id = _uuid.UUID(str(request.data.get("client_request_id")))
        except ValueError:
            request_id = None  # the serializer below reports the invalid id
        done = Meal.objects.filter(user=request.user, client_request_id=request_id).prefetch_related("items").first() \
            if request_id else None
        if done:
            return Response(_meal_json(done), status=status.HTTP_200_OK)
        data = dict(request.data)
        data.setdefault("kind", source.kind)
        data.setdefault("time_zone", source.time_zone)
        from django.utils import timezone as _tz
        data.setdefault("occurred_at", _tz.now().isoformat())
        data["items"] = [{"food_id": item_food_id(item), "grams": str(item.grams)} for item in source.items.all()]
        data.setdefault("notes", "Same as usual")
        serializer = MealInputSerializer(data=data)
        serializer.is_valid(raise_exception=True)
        meal, created = create_meal(request.user, serializer.validated_data)
        return Response(_meal_json(meal), status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)
