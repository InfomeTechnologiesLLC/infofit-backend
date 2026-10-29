"""Plain-language BMI categories and gentle, practical next steps. General wellness guidance, not medical advice."""

LABELS = {"underweight": "Underweight", "healthy_range": "Healthy range", "overweight": "Overweight",
          "obesity": "Obesity"}

ADVICE = {
    "underweight": [
        "Eat three regular meals and add one or two healthy snacks, such as nuts, curd, eggs, dal or a banana.",
        "Include some protein at every meal: dal, paneer, eggs, fish, chicken or sprouts.",
        "Strength exercises two or three times a week help you build muscle, not just weight.",
        "If you have lost weight without trying, please talk to a doctor.",
    ],
    "healthy_range": [
        "Nice work. Keep reaching your daily step goal on most days.",
        "Keep a balanced plate: half vegetables, a quarter protein, a quarter whole grains.",
        "Drink water through the day and aim for 7 to 8 hours of sleep.",
    ],
    "overweight": [
        "Add a little movement each day: try 1,000 more steps than your usual, then build up.",
        "Fill half your plate with vegetables, choose whole grains, and swap sugary drinks for water.",
        "Aim for slow, steady change, about 0.25 to 0.5 kg a week. Crash diets rarely last.",
        "Strength exercises twice a week help, and good sleep makes healthy eating easier.",
    ],
    "obesity": [
        "Start small: a 10-minute walk after meals is a great first step.",
        "Fill half your plate with vegetables, choose whole grains, and swap sugary drinks for water.",
        "Aim for slow, steady change, about 0.5 kg a week, and celebrate habits, not just the scale.",
        "A doctor can help you make a plan that suits you and check things like blood pressure and sugar.",
    ],
}

ASIAN_NOTE = ("For people of South Asian background, health risks can start at a lower BMI, from about 23. "
              "BMI also does not account for muscle, age or body build.")


def category(bmi):
    if bmi < 18.5:
        return "underweight"
    if bmi < 25:
        return "healthy_range"
    if bmi < 30:
        return "overweight"
    return "obesity"


def healthy_weight_range(height_cm):
    """(low_kg, high_kg) for BMI 18.5-24.9 at this height."""
    metres = float(height_cm) / 100.0
    return round(18.5 * metres * metres, 1), round(24.9 * metres * metres, 1)


def summary(height_cm, bmi):
    cat = category(bmi)
    low, high = healthy_weight_range(height_cm)
    return {"category": cat, "category_label": LABELS[cat], "healthy_weight_min_kg": low,
            "healthy_weight_max_kg": high, "advice": ADVICE[cat], "context_note": ASIAN_NOTE}
