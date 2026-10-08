# Generated for INFOME FIT Phase 11.
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True
    dependencies = [migrations.swappable_dependency(settings.AUTH_USER_MODEL)]

    operations = [
        migrations.CreateModel(
            name="WellnessProgram",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("title", models.CharField(max_length=120)),
                ("summary", models.CharField(max_length=280)),
                ("daily_action", models.TextField(max_length=1000)),
                ("focus_goal", models.CharField(choices=[("all", "All goals"), ("general", "General wellness"), ("weight_loss", "Weight loss"), ("muscle_gain", "Muscle gain"), ("endurance", "Endurance"), ("stress", "Stress & sleep")], default="all", max_length=20)),
                ("start_date", models.DateField()),
                ("end_date", models.DateField()),
                ("is_published", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.CreateModel(
            name="ProgramEnrollment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("joined_at", models.DateTimeField(auto_now_add=True)),
                ("program", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="enrollments", to="wellness.wellnessprogram")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="wellness_enrollments", to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name="ProgramCheckIn",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("local_date", models.DateField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("enrollment", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="check_ins", to="wellness.programenrollment")),
            ],
        ),
        migrations.AddConstraint(
            model_name="programenrollment",
            constraint=models.UniqueConstraint(fields=("program", "user"), name="uniq_wellness_enrollment"),
        ),
        migrations.AddConstraint(
            model_name="programcheckin",
            constraint=models.UniqueConstraint(fields=("enrollment", "local_date"), name="uniq_wellness_checkin_day"),
        ),
    ]
