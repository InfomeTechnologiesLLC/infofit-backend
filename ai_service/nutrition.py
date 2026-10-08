"""USDA FoodData Central lookup for employee-confirmed food names."""
import json
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings
from rest_framework.exceptions import APIException


class NutritionUnavailable(APIException):
    status_code = 503
    default_detail = "Food calorie lookup is unavailable right now."
    default_code = "nutrition_unavailable"


def _energy_kcal(food):
    nutrients = food.get("foodNutrients") or []
    # Foundation foods use 2047/2048; other FDC datasets normally use 1008.
    for nutrient_id in (1008, 2048, 2047):
        for row in nutrients:
            if row.get("nutrientId") != nutrient_id:
                continue
            if str(row.get("unitName", "")).upper() != "KCAL":
                continue
            value = row.get("value")
            if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1000:
                return round(float(value), 1)
    return None


def search_foods(query):
    if not settings.USDA_FDC_API_KEY:
        raise NutritionUnavailable("Food calorie lookup is not configured on this server.")
    url = "https://api.nal.usda.gov/fdc/v1/foods/search?" + urllib.parse.urlencode(
        {"api_key": settings.USDA_FDC_API_KEY}
    )
    body = json.dumps({
        "query": query,
        "dataType": ["Survey (FNDDS)", "Foundation", "SR Legacy"],
        "pageSize": 15,
    }).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "User-Agent": "INFOME-FIT/1.0",
    })
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.load(response)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        raise NutritionUnavailable() from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("foods"), list):
        raise NutritionUnavailable()
    results = []
    for food in payload.get("foods", []):
        if not isinstance(food, dict):
            continue
        kcal = _energy_kcal(food)
        if kcal is None or not isinstance(food.get("fdcId"), int):
            continue
        results.append({
            "fdc_id": food["fdcId"],
            "description": food.get("description", ""),
            "data_type": food.get("dataType", ""),
            "kcal_per_100g": kcal,
        })
    return results[:10]


def food_details(fdc_id):
    """Load a selected USDA food again on the server before saving a meal."""
    if not settings.USDA_FDC_API_KEY:
        raise NutritionUnavailable("Food lookup is not configured on this server.")
    url = "https://api.nal.usda.gov/fdc/v1/food/" + str(fdc_id) + "?" + urllib.parse.urlencode(
        {"api_key": settings.USDA_FDC_API_KEY}
    )
    request = urllib.request.Request(url, headers={"User-Agent": "INFOME-FIT/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            food = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            from rest_framework.exceptions import ValidationError
            raise ValidationError({"items": "A selected food is no longer available in USDA."}) from exc
        raise NutritionUnavailable() from exc
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        raise NutritionUnavailable() from exc
    if not isinstance(food, dict) or food.get("fdcId") != fdc_id:
        raise NutritionUnavailable("USDA returned an unexpected food record.")
    nutrients = {}
    for row in food.get("foodNutrients") or []:
        if not isinstance(row, dict):
            continue
        meta = row.get("nutrient") or row
        nutrient_id = meta.get("id", meta.get("nutrientId"))
        unit = str(meta.get("unitName", "")).upper()
        value = row.get("amount", row.get("value"))
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            continue
        nutrients[(nutrient_id, unit)] = float(value)
    kcal = next((nutrients[(id_, "KCAL")] for id_ in (1008, 2048, 2047)
                 if (id_, "KCAL") in nutrients and nutrients[(id_, "KCAL")] <= 1000), None)
    if kcal is None:
        from rest_framework.exceptions import ValidationError
        raise ValidationError({"items": "A selected food has no reliable kcal value in USDA."})
    return {
        "fdc_id": fdc_id,
        "description": str(food.get("description") or "")[:255],
        "data_type": str(food.get("dataType") or "")[:40],
        "kcal_per_100g": kcal,
        "protein_per_100g": nutrients.get((1003, "G")),
        "carbohydrate_per_100g": nutrients.get((1005, "G")),
        "fat_per_100g": nutrients.get((1004, "G")),
    }
