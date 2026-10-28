"""Rebuild indian_foods.csv from the two open datasets. Developer tool only; the server reads the CSV.

    pip install openpyxl
    python build_indian_foods.py <path to INDB.xlsx> <path to IFCT index.csv> > indian_foods.csv

Sources
- INDB: Indian Nutrient Databank, Vijayaraghavan R. et al., Current Developments in Nutrition 2024 (CC BY 4.0 article),
  https://github.com/lindsayjaacks/Indian-Nutrient-Databank-INDB- . 1,014 cooked recipes, values per 100 g and per serving.
- IFCT 2017: Indian Food Composition Tables, Longvah T. et al., ICMR-NIN 2017, as packaged in the MIT-licensed npm
  package @ifct2017/compositions. Raw ingredients; energy is given in kJ and converted here (1 kcal = 4.184 kJ).
"""
import csv
import re
import sys

import openpyxl

# INDB recipes include every drop of deep-frying oil as eaten, so many fried dishes come out at 60-90 g fat per
# 100 g (about double their real energy). Those rows are left out; such dishes fall back to USDA or IFCT matches.
MAX_RECIPE_FAT_G = 35

FIELDS = ["source", "code", "name", "local_names", "kcal_per_100g", "protein_per_100g",
          "carbohydrate_per_100g", "fat_per_100g", "serving_g", "serving_unit"]


def num(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def fmt(value, digits=2):
    return "" if value is None else f"{value:.{digits}f}"


def indb_rows(path):
    wb = openpyxl.load_workbook(path, read_only=True)
    rows = wb.worksheets[0].iter_rows(values_only=True)
    header = next(rows)
    col = {name: i for i, name in enumerate(header)}
    for row in rows:
        code, name, kcal = row[col["food_code"]], row[col["food_name"]], num(row[col["energy_kcal"]])
        if not code or not name or kcal is None or kcal > 1000:
            continue
        fat = num(row[col["fat_g"]])
        if fat is not None and fat > MAX_RECIPE_FAT_G:
            continue
        serving_kcal = num(row[col["unit_serving_energy_kcal"]])
        serving_g = serving_kcal / kcal * 100 if serving_kcal and kcal else None
        if serving_g is not None and not 1 <= serving_g <= 1500:
            serving_g = None
        # "Semolina dosa (Suji/Rava dosa)" -> local names "Suji", "Rava dosa"
        local = "; ".join(part.strip() for part in re.findall(r"\(([^)]*)\)", name))
        local = local.replace("/", "; ")
        yield {"source": "indb", "code": code, "name": name.strip(), "local_names": local,
               "kcal_per_100g": fmt(kcal), "protein_per_100g": fmt(num(row[col["protein_g"]])),
               "carbohydrate_per_100g": fmt(num(row[col["carb_g"]])), "fat_per_100g": fmt(num(row[col["fat_g"]])),
               "serving_g": fmt(serving_g, 0), "serving_unit": str(row[col["servings_unit"]] or "").strip()}


def ifct_rows(path):
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        col = {name.split("; ")[-1]: i for i, name in enumerate(header)}
        for row in reader:
            kj = num(row[col["enerc"]])
            if kj is None:
                continue
            kcal = kj / 4.184
            if kcal > 1000:
                continue
            # "A. Moricha guti; H. Ramdana" -> "Moricha guti; Ramdana" (language prefixes removed)
            names = [re.sub(r"^[A-Z][a-z]*\.(,? ?[A-Z][a-z]*\.)*\s*", "", part.strip())
                     for part in row[col["lang"]].split(";")]
            local = "; ".join(dict.fromkeys(n for n in names if n))
            yield {"source": "ifct", "code": row[col["code"]], "name": row[col["name"]].strip(), "local_names": local,
                   "kcal_per_100g": fmt(kcal), "protein_per_100g": fmt(num(row[col["protcnt"]])),
                   "carbohydrate_per_100g": fmt(num(row[col["choavldf"]])), "fat_per_100g": fmt(num(row[col["fatce"]])),
                   "serving_g": "", "serving_unit": ""}


if __name__ == "__main__":
    writer = csv.DictWriter(sys.stdout, FIELDS)
    writer.writeheader()
    for item in indb_rows(sys.argv[1]):
        writer.writerow(item)
    for item in ifct_rows(sys.argv[2]):
        writer.writerow(item)
