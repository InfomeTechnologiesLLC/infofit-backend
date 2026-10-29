from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import transaction

from accounts.models import ADMIN_ROLES, Department, EmployeeProfile, Role, Team, User
from challenges.models import Challenge
from wellness.models import WellnessProgram


class BootstrapFormMixin:
    def style_fields(self):
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = "form-check-input"
            else:
                field.widget.attrs["class"] = "form-select" if isinstance(field.widget, forms.Select) else "form-control"


class PortalLoginForm(BootstrapFormMixin, AuthenticationForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].label = "Email"
        self.style_fields()

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if not user.is_staff or not (user.is_superuser or user.role in ADMIN_ROLES):
            raise ValidationError("This account cannot access the staff portal.", code="portal_access")


class EmployeeForm(BootstrapFormMixin, forms.Form):
    email = forms.EmailField(max_length=254)
    first_name = forms.CharField(max_length=100, required=False)
    last_name = forms.CharField(max_length=100, required=False)
    employee_id = forms.CharField(max_length=32, required=False)
    department = forms.ModelChoiceField(queryset=Department.objects.all(), required=False)
    team = forms.ModelChoiceField(queryset=Team.objects.all(), required=False)
    designation = forms.CharField(max_length=120, required=False)
    branch = forms.CharField(max_length=120, required=False)
    is_active = forms.BooleanField(required=False, initial=True)
    password1 = forms.CharField(label="Initial password", widget=forms.PasswordInput, required=False)
    password2 = forms.CharField(label="Confirm password", widget=forms.PasswordInput, required=False)

    def __init__(self, *args, instance=None, **kwargs):
        self.instance = instance
        initial = kwargs.pop("initial", {})
        if instance:
            profile = instance.profile
            initial.update({
                "email": instance.email, "first_name": instance.first_name,
                "last_name": instance.last_name, "employee_id": instance.employee_id,
                "department": profile.department_id, "team": profile.team_id,
                "designation": profile.designation, "branch": profile.branch,
                "is_active": instance.is_active,
            })
        super().__init__(*args, initial=initial, **kwargs)
        self.style_fields()
        if instance:
            self.fields.pop("password1")
            self.fields.pop("password2")

    def clean_email(self):
        value = User.objects.normalize_email(self.cleaned_data["email"]).lower()
        existing = User.objects.filter(email=value)
        if self.instance:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise forms.ValidationError("This email is already in use.")
        return value

    def clean_employee_id(self):
        value = self.cleaned_data["employee_id"].strip() or None
        if value:
            existing = User.objects.filter(employee_id=value)
            if self.instance:
                existing = existing.exclude(pk=self.instance.pk)
            if existing.exists():
                raise forms.ValidationError("This employee ID is already in use.")
        return value

    def clean(self):
        data = super().clean()
        data["target_steps"] = data.get("target_steps") or 0
        data["target_running_minutes"] = data.get("target_running_minutes") or 0
        if not self.instance:
            if not data.get("password1") or data.get("password1") != data.get("password2"):
                raise forms.ValidationError("Enter the same initial password twice.")
            if data.get("password1"):
                validate_password(data["password1"])
        team = data.get("team")
        department = data.get("department")
        if team and team.department_id and department and team.department_id != department.pk:
            self.add_error("team", "This team belongs to a different department.")
        return data

    @transaction.atomic
    def save(self):
        data = self.cleaned_data
        if self.instance:
            user = self.instance
        else:
            user = User.objects.create_user(data["email"], data["password1"], role=Role.EMPLOYEE)
        for field in ("email", "first_name", "last_name", "employee_id", "is_active"):
            setattr(user, field, data[field])
        user.save()
        profile, _ = EmployeeProfile.objects.get_or_create(user=user)
        profile.team = data["team"]
        profile.department = data["department"] or (data["team"].department if data["team"] else None)
        profile.designation = data["designation"]
        profile.branch = data["branch"]
        profile.save()
        return user


class EmployeePasswordForm(BootstrapFormMixin, forms.Form):
    password1 = forms.CharField(label="New password", strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))
    password2 = forms.CharField(label="Type it again", strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))
    sign_out = forms.BooleanField(label="Sign them out of the app on every phone", required=False, initial=True,
        help_text="They will need the new password to sign in again.")

    def __init__(self, *args, employee=None, **kwargs):
        self.employee = employee
        super().__init__(*args, **kwargs)
        self.style_fields()

    def clean(self):
        data = super().clean()
        first, second = data.get("password1"), data.get("password2")
        if first and second and first != second:
            self.add_error("password2", "The two passwords don't match.")
        elif first:
            try:
                validate_password(first, user=self.employee)
            except ValidationError as error:
                self.add_error("password1", error)
        return data


class DepartmentForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Department
        fields = ("name",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.style_fields()


class TeamForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Team
        fields = ("name", "description", "department", "manager")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["manager"].queryset = User.objects.filter(is_active=True, role__in=[Role.TEAM_MANAGER, *ADMIN_ROLES])
        self.style_fields()


# What HR reads under the type picker, per challenge kind (also used by the form's script).
CHALLENGE_KIND_HELP = {
    "steps": "Total steps recorded by the phone or watch.",
    "running": "Minutes of recorded running workouts (outdoor or treadmill). Walking steps never count.",
    "distance": "Total kilometres walked or run, recorded by the phone or watch.",
    "active_minutes": "Minutes of any recorded workout of 10 minutes or more (walks, gym, sports, cycling...).",
    "workout": "Minutes of one chosen activity, e.g. cycling, swimming or yoga, recorded as a workout.",
    "calories": "Active calories burned, from a watch or fitness app. Phones without one may record none.",
    "step_days": "Number of days someone reaches a daily step goal. Rewards consistency, not just big days.",
    "sleep_nights": "Number of nights with enough sleep, recorded by a watch or sleep-tracking app.",
}


class ChallengeForm(BootstrapFormMixin, forms.ModelForm):
    """One form for every challenge kind; the page script shows only the fields the chosen kind uses."""

    class Meta:
        model = Challenge
        fields = ("title", "slug", "description", "kind", "activity_type", "target_steps", "target_running_minutes",
                  "target_value", "daily_threshold", "start_date", "end_date", "reward_points", "badge_title",
                  "is_published")
        widgets = {"start_date": forms.DateInput(attrs={"type": "date"}),
                   "end_date": forms.DateInput(attrs={"type": "date"}),
                   "description": forms.Textarea(attrs={"rows": 3})}
        labels = {"kind": "Challenge type", "activity_type": "Which activity counts", "target_value": "Target",
                  "daily_threshold": "Daily minimum", "slug": "Short code (unique, used in links)",
                  "is_published": "Published (visible in the app)"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("target_steps", "target_running_minutes", "target_value", "daily_threshold", "activity_type"):
            self.fields[name].required = False
        self.fields["kind"].help_text = "Every type is scored automatically from the phone or watch. Entries typed in by hand never count."
        self.fields["target_steps"].help_text = "Total steps to reach. Steps from the joining day do not count."
        self.fields["target_running_minutes"].help_text = "Minutes of running workouts recorded after joining."
        self.fields["target_value"].help_text = "In the unit shown."
        self.fields["daily_threshold"].help_text = "Steps per day, or minutes of sleep per night (420 = 7 hours)."
        self.fields["reward_points"].help_text = "Given automatically when someone reaches the target, and taken back if their data is later corrected."
        self.fields["is_published"].help_text = ("When you publish, every employee gets a notification about it. "
                                                 "After that the code, dates, target, points and badge are locked.")
        if not self.instance.pk:
            self.initial.setdefault("is_published", True)
        self.style_fields()
        self.fields["kind"].widget.attrs["data-kind-select"] = "1"
        if self.instance.pk and (self.instance.is_published or self.instance.enrollments.exists()):
            for name in ("slug", "start_date", "end_date", "target_steps", "reward_points", "badge_title"):
                self.fields[name].disabled = True

    def clean(self):
        data = super().clean()
        kind = data.get("kind")
        if kind == Challenge.Kind.RUNNING and not data.get("target_running_minutes"):
            self.add_error("target_running_minutes", "Enter a running time target.")
        if kind == Challenge.Kind.STEPS and not data.get("target_steps"):
            self.add_error("target_steps", "Enter a step target.")
        if kind not in (Challenge.Kind.STEPS, Challenge.Kind.RUNNING, None) and not data.get("target_value"):
            self.add_error("target_value", "Enter a target.")
        if kind == Challenge.Kind.WORKOUT and not data.get("activity_type"):
            self.add_error("activity_type", "Choose which activity counts.")
        if kind in (Challenge.Kind.STEP_DAYS, Challenge.Kind.SLEEP_NIGHTS) and not data.get("daily_threshold"):
            self.add_error("daily_threshold", "Enter the daily minimum.")
        if kind != Challenge.Kind.WORKOUT:
            data["activity_type"] = ""
        for name in ("target_steps", "target_running_minutes", "target_value", "daily_threshold"):
            if data.get(name) is None:
                data[name] = 0
        if (not self.instance.pk and kind in (Challenge.Kind.STEPS, Challenge.Kind.DISTANCE, Challenge.Kind.CALORIES,
                                              Challenge.Kind.STEP_DAYS, Challenge.Kind.SLEEP_NIGHTS) and
                data.get("start_date") and data.get("start_date") == data.get("end_date")):
            self.add_error("end_date", "This type needs at least two dates, because the joining day cannot be counted.")
        return data


class WellnessProgramForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = WellnessProgram
        fields = ("title", "summary", "daily_action", "focus_goal", "start_date", "end_date", "is_published")
        widgets = {"start_date": forms.DateInput(attrs={"type": "date"}),
                   "end_date": forms.DateInput(attrs={"type": "date"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.style_fields()
        self.fields["is_published"].label = "Published (visible in the app)"
        self.fields["is_published"].help_text = "When you publish, every employee gets a notification about it."
        if not self.instance.pk:
            self.initial.setdefault("is_published", True)
        if self.instance.pk and self.instance.enrollments.exists():
            for name in ("focus_goal", "start_date", "end_date"):
                self.fields[name].disabled = True
