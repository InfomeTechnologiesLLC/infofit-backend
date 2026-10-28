"""One food lookup for the whole app: the local Indian database first, then USDA FoodData Central.

Every result carries a `food_id` the app sends back when saving a meal:
  "indb:ASC144"  Indian Nutrient Databank recipe (cooked dish)
  "ifct:A003"    IFCT 2017 raw ingredient
  "usda:123456"  USDA FoodData Central food (also sent as `fdc_id` for older app versions)
Values are per 100 g. Nothing here is a measurement of what a person ate; portions are their own entry.
"""
import csv
import re
from decimal import Decimal
from pathlib import Path

from django.db import transaction
from rest_framework.exceptions import ValidationError

from ai_service.nutrition import NutritionUnavailable, food_details, search_foods as usda_search

DATA_FILE = Path(__file__).resolve().parent / "data" / "indian_foods.csv"
SOURCE_LABELS = {"indb": "Indian dish (INDB)", "ifct": "Indian ingredient (IFCT 2017)", "usda": "USDA"}
_WORD = re.compile(r"[a-z0-9]+")


def _words(text):
    return _WORD.findall(str(text).lower())


def _search_text(name, local_names):
    return " ".join(_words(f"{name} {local_names}"))[:800]


def _decimal(value):
    value = (value or "").strip()
    return Decimal(value) if value else None


def load_food_csv(food_model=None, path=DATA_FILE):
    """Insert or update every row of the bundled CSV. Safe to run repeatedly. Returns the row count."""
    if food_model is None:
        from .models import FoodItem as food_model
    count = 0
    with open(path, newline="", encoding="utf-8") as handle, transaction.atomic():
        for row in csv.DictReader(handle):
            serving = (row.get("serving_g") or "").strip()
            food_model.objects.update_or_create(
                source=row["source"], code=row["code"],
                defaults={
                    "name": row["name"][:255], "local_names": row["local_names"][:500],
                    "search_text": _search_text(row["name"], row["local_names"]),
                    "kcal_per_100g": _decimal(row["kcal_per_100g"]),
                    "protein_per_100g": _decimal(row["protein_per_100g"]),
                    "carbohydrate_per_100g": _decimal(row["carbohydrate_per_100g"]),
                    "fat_per_100g": _decimal(row["fat_per_100g"]),
                    "serving_g": int(float(serving)) if serving else None,
                    "serving_unit": row["serving_unit"][:40],
                })
            count += 1
    return count


STOP_WORDS = {"a", "an", "the", "of", "with", "and", "my", "some", "plate", "plates", "bowl", "bowls", "cup", "cups",
              "glass", "glasses", "piece", "pieces", "serving", "servings", "small", "large", "big", "medium", "one",
              "two", "three", "four", "half", "full", "homemade", "home", "made"}
# Forms of a food people rarely mean unless they say so ("rasam" is the dish, not rasam powder).
FORM_WORDS = {"powder", "mix", "flour", "stock", "pickle", "concentrate", "premix"}


RAW_WORDS = {"raw", "flour", "dry", "uncooked", "atta", "grain", "grains"}
# What people usually mean by a one-word everyday food, so "rice" is cooked rice rather than rice upma.
PREFERRED = {
    "rice": "indb:ASC113", "chawal": "indb:ASC113", "tea": "indb:ASC001", "chai": "indb:ASC001",
    "coffee": "indb:BFP008", "egg": "ifct:M004", "boiled egg": "ifct:M004", "omelette": "ifct:M007",
    "omelet": "ifct:M007", "oats": "indb:ASC050", "dal": "indb:ASC155", "roti": "indb:ASC096",
    "chapati": "indb:ASC096", "paratha": "indb:ASC097", "parantha": "indb:ASC097", "lassi": "indb:ASC022",
    "pulao": "indb:ASC114", "dosa": "indb:BFP148", "milk": "ifct:L002",
}
# IFCT has no serving sizes; these everyday ones let "two eggs" or "a glass of milk" be logged by count.
SERVING_HINTS = {"ifct:M001": (50, "egg"), "ifct:M004": (50, "egg"), "ifct:M007": (60, "omelette"),
                 "ifct:L001": (200, "glass"), "ifct:L002": (200, "glass")}


def _stem(word):
    if len(word) > 3 and word.endswith("es"):
        return word[:-2]
    if len(word) > 3 and word.endswith("s"):
        return word[:-1]
    return word


def _query_words(query):
    words = [w for w in _words(query) if not w.isdigit() and w not in STOP_WORDS]
    return words or [w for w in _words(query) if not w.isdigit()]


def _word_score(word, name_words, text_words):
    forms = {word}
    if len(word) > 3 and word.endswith("es"):
        forms.add(word[:-2])
    if len(word) > 3 and word.endswith("s"):
        forms.add(word[:-1])
    if forms & set(name_words):
        return 6
    if forms & set(text_words):
        return 4
    if len(word) >= 4 and any(t.startswith(f) for t in text_words for f in forms):
        return 2
    return 0


def _score(query_words, food):
    """(matched every word?, score). Whole-word matches beat partial ones; dishes beat raw ingredients."""
    text_words = food.search_text.split()
    name_words = _words(food.name)
    scores = [_word_score(word, name_words, text_words) for word in query_words]
    matched = sum(1 for value in scores if value)
    if not matched or matched * 2 < len(query_words):
        return False, 0
    # In "chicken biryani" the dish is the last word, so it counts for more than the first.
    score = sum(scores) + scores[-1] * 0.5
    # "Egg, poultry, whole" or "Banana, ripe" is what someone means by "egg" or "banana".
    head = _words(re.split(r"[,(]", food.name, maxsplit=1)[0])
    if head and {_stem(w) for w in head} == {_stem(w) for w in query_words}:
        score += 5
    # IFCT cereals (A) and pulses (B) are raw grain, about 2.5 times the energy of the cooked food people eat.
    if food.source == "ifct" and food.code[:1] in ("A", "B") and not RAW_WORDS & set(query_words):
        score -= 8
    if name_words and _word_score(query_words[0], name_words[:1], name_words[:1]):
        score += 2
    if food.source == "indb":
        score += 1
    if FORM_WORDS & set(name_words) and not FORM_WORDS & set(query_words):
        score -= 5
    return matched == len(query_words), score - len(name_words) * 0.2


def local_search(query, limit=10, include_partial=True):
    from django.db.models import Q

    from .models import FoodItem
    words = _query_words(query)
    if not words:
        return []
    condition = Q()
    for word in words:
        stem = _stem(word)
        condition |= Q(search_text__icontains=stem[:4] if len(stem) > 4 else stem)
    preferred = PREFERRED.get(" ".join(_stem(w) for w in words)) or PREFERRED.get(" ".join(words))
    ranked = []
    for food in FoodItem.objects.filter(condition):
        full, score = _score(words, food)
        if preferred and food.food_id == preferred:
            score += 20
        if score > 0 and (full or include_partial):
            ranked.append((not full, -score, food.name, food))
    ranked.sort(key=lambda row: row[:3])
    return [{**local_payload(food), "full_match": not partial} for partial, _, _, food in ranked[:limit]]


def local_payload(food):
    serving_g, serving_unit = food.serving_g, food.serving_unit or None
    if serving_g is None and food.food_id in SERVING_HINTS:
        serving_g, serving_unit = SERVING_HINTS[food.food_id]
    return {
        "food_id": food.food_id, "fdc_id": None, "source": food.source,
        "description": food.name, "data_type": SOURCE_LABELS.get(food.source, food.source),
        "kcal_per_100g": float(food.kcal_per_100g),
        "serving_g": serving_g, "serving_unit": serving_unit,
    }


def usda_payload(row):
    return {**row, "food_id": f"usda:{row['fdc_id']}", "source": "usda", "serving_g": None, "serving_unit": None}


def search(query, limit=10):
    """Full local matches, then USDA, then partial local matches.

    USDA being down or unconfigured never hides local results.
    """
    local = local_search(query, limit)
    full = [row for row in local if row["full_match"]]
    partial = [row for row in local if not row["full_match"]]
    remote = []
    if len(full) < limit:
        try:
            remote = [usda_payload(r) for r in usda_search(query)]
        except NutritionUnavailable:
            if not local:
                raise
    return [{k: v for k, v in row.items() if k != "full_match"} for row in (full + remote + partial)][:limit]


def best_match(query):
    """The single most likely food for an IVA or Lens draft, or None."""
    results = search(query, limit=3)
    return results[0] if results else None


def parse_food_id(food_id):
    if not isinstance(food_id, str) or ":" not in food_id:
        raise ValidationError({"items": "Unknown food reference."})
    source, code = food_id.split(":", 1)
    if source not in SOURCE_LABELS or not code or len(code) > 20:
        raise ValidationError({"items": "Unknown food reference."})
    return source, code


def resolve(food_id=None, fdc_id=None):
    """Load a food again on the server before saving, so the app can never supply its own calorie values."""
    if fdc_id is not None and food_id is None:
        food_id = f"usda:{fdc_id}"
    source, code = parse_food_id(food_id)
    if source == "usda":
        if not code.isdigit():
            raise ValidationError({"items": "Unknown food reference."})
        details = food_details(int(code))
        return {**details, "source": "usda", "code": code}
    from .models import FoodItem
    food = FoodItem.objects.filter(source=source, code=code).first()
    if not food:
        raise ValidationError({"items": "A selected food is no longer available."})
    as_float = lambda value: float(value) if value is not None else None  # noqa: E731
    return {"fdc_id": None, "source": source, "code": code, "description": food.name[:255],
            "data_type": SOURCE_LABELS[source][:40], "kcal_per_100g": float(food.kcal_per_100g),
            "protein_per_100g": as_float(food.protein_per_100g),
            "carbohydrate_per_100g": as_float(food.carbohydrate_per_100g),
            "fat_per_100g": as_float(food.fat_per_100g)}
