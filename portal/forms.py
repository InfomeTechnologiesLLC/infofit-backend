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


class ChallengeForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Challenge
        fields = ("slug", "title", "description", "kind", "start_date", "end_date", "target_steps", "target_running_minutes", "reward_points", "badge_title", "is_published")
        widgets = {"start_date": forms.DateInput(attrs={"type": "date"}),
                   "end_date": forms.DateInput(attrs={"type": "date"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["target_steps"].required = False
        self.fields["target_running_minutes"].required = False
        self.fields["kind"].help_text = "Running uses recorded Health Connect running sessions, not steps. Changing a published challenge recalculates points."
        self.fields["target_steps"].help_text = "For a steps challenge only. Steps from the day you join do not count."
        self.fields["target_running_minutes"].help_text = "For a running challenge only. Sessions must start after enrollment."
        self.style_fields()
        if self.instance.pk and (self.instance.is_published or self.instance.enrollments.exists()):
            for name in ("slug", "start_date", "end_date", "target_steps", "reward_points", "badge_title"):
                self.fields[name].disabled = True

    def clean(self):
        data = super().clean()
        if data.get("kind") == Challenge.Kind.RUNNING and not data.get("target_running_minutes"):
            self.add_error("target_running_minutes", "Enter a running time target.")
        if data.get("kind") == Challenge.Kind.STEPS and not data.get("target_steps"):
            self.add_error("target_steps", "Enter a step target.")
        if (not self.instance.pk and data.get("kind") == Challenge.Kind.STEPS and
                data.get("start_date") == data.get("end_date")):
            self.add_error("end_date", "A step challenge needs at least two dates because joining-day steps cannot be verified separately.")
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
        if self.instance.pk and self.instance.enrollments.exists():
            for name in ("focus_goal", "start_date", "end_date"):
                self.fields[name].disabled = True
