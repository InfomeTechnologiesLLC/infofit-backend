"""Session-authenticated staff operations, separate from the Django admin UI."""
import hashlib
from datetime import date, timedelta
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Max, Q, Sum
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.decorators.cache import never_cache
from django.core.paginator import Paginator

from accounts.models import ADMIN_ROLES, Department, Role, Team, User
from challenges.models import Challenge, ChallengeEnrollment, ChallengeProgress, PointsLedger
from challenges.services import reconcile_user
from community.models import CommunityPost, CommunityReport
from fitness.models import DailyActivity, FitnessAccount, FitnessSource
from notifications.models import PushDelivery
from nutrition.models import Meal
from wellness.models import HydrationLog, ProgramCheckIn, ProgramEnrollment, WellnessProgram

from .forms import (ChallengeForm, DepartmentForm, EmployeeForm, PortalLoginForm,
                    TeamForm, WellnessProgramForm)
from .insights import points_totals, staff_rankings, step_period, step_totals


class PortalLoginView(LoginView):
    template_name = "portal/login.html"
    authentication_form = PortalLoginForm
    redirect_authenticated_user = False

    def get_success_url(self):
        return reverse("portal:dashboard")

    def post(self, request, *args, **kwargs):
        # Bound to the submitted email and direct peer IP, without trusting proxy headers.
        identity = f"{request.META.get('REMOTE_ADDR', '')}:{request.POST.get('username', '').strip().lower()}"
        key = "portal_login_" + hashlib.sha256(identity.encode()).hexdigest()
        attempts = cache.get(key, 0)
        if attempts >= 10:
            return HttpResponse("Too many login attempts. Try again in ten minutes.", status=429)
        response = super().post(request, *args, **kwargs)
        if response.status_code == 302:
            cache.delete(key)
        else:
            cache.set(key, attempts + 1, 600)
        return response


def staff_only(view):
    @wraps(view)
    @login_required(login_url="portal:login")
    def wrapped(request, *args, **kwargs):
        user = request.user
        if not (user.is_active and user.is_staff and (user.is_superuser or user.role in ADMIN_ROLES)):
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapped


def page_context(title, section, **extra):
    return {"title": title, "section": section, **extra}


@staff_only
@never_cache
def dashboard(request):
    today = timezone.localdate()
    recent = timezone.now() - timedelta(days=7)
    employee_filter = {"user__role": Role.EMPLOYEE, "user__is_active": True}
    challenge_participants = set(ChallengeEnrollment.objects.filter(
        **employee_filter, challenge__is_published=True,
        challenge__start_date__lte=today, challenge__end_date__gte=today,
    ).values_list("user_id", flat=True))
    program_participants = set(ProgramEnrollment.objects.filter(
        **employee_filter, program__is_published=True,
        program__start_date__lte=today, program__end_date__gte=today,
    ).values_list("user_id", flat=True))
    evaluated = set(ChallengeProgress.objects.filter(
        enrollment__user__role=Role.EMPLOYEE, enrollment__user__is_active=True,
        evaluated_at__gte=recent,
    ).filter(Q(has_step_data=True) | Q(verified_running_minutes__gt=0)).values_list("enrollment__user_id", flat=True))
    checked_in = set(ProgramCheckIn.objects.filter(
        enrollment__user__role=Role.EMPLOYEE, enrollment__user__is_active=True,
        created_at__gte=recent,
    ).values_list("enrollment__user_id", flat=True))
    cards = [
        ("Active employees", User.objects.filter(role=Role.EMPLOYEE, is_active=True).count()),
        ("Participating now", len(challenge_participants | program_participants)),
        ("Engaged in last 7 days", len(evaluated | checked_in)),
        ("Challenge completions", ChallengeProgress.objects.filter(is_complete=True,
            enrollment__user__role=Role.EMPLOYEE).count()),
        ("Teams", Team.objects.count()),
        ("Sources synced in 7 days", FitnessSource.objects.filter(last_successful_sync_at__gte=recent).count()),
        ("Published posts", CommunityPost.objects.filter(status=CommunityPost.Status.PUBLISHED).count()),
        ("Saved meals", Meal.objects.count()),
        ("Open reports", CommunityReport.objects.filter(status=CommunityReport.Status.OPEN).count()),
        ("Pushes awaiting delivery", PushDelivery.objects.filter(state__in=[PushDelivery.State.PENDING,
            PushDelivery.State.RETRY]).count()),
    ]
    active_ids = list(User.objects.filter(role=Role.EMPLOYEE, is_active=True).values_list("pk", flat=True))
    today_steps = step_totals(active_ids, today, today)
    cards.insert(2, ("Employees with synced steps today", len(today_steps)))
    cards.insert(3, ("Verified steps today", sum(row["steps"] for row in today_steps.values())))
    recent_reports = CommunityReport.objects.filter(status=CommunityReport.Status.OPEN).select_related(
        "post", "post__author", "reporter").order_by("-created_at")[:5]
    return render(request, "portal/dashboard.html", page_context("Overview", "dashboard",
        cards=cards, recent_reports=recent_reports, today=today, recent=recent,
        top_staff=staff_rankings()[:3], top_steps=[row for row in staff_rankings("steps") if row["step_days"]][:3]))


@staff_only
@never_cache
def points_leaderboard(request):
    sort = request.GET.get("sort", "points")
    if sort not in {"points", "steps", "challenges"}:
        sort = "points"
    period = request.GET.get("period", "month")
    if period not in {"week", "month", "all"}:
        period = "month"
    return render(request, "portal/points.html", page_context(
        "Individual rankings", "points", rows=staff_rankings(sort, period), sort=sort, period=period))


@staff_only
@never_cache
def employees(request):
    query = request.GET.get("q", "").strip()[:100]
    recent = timezone.now() - timedelta(days=7)
    queryset = User.objects.filter(role=Role.EMPLOYEE).select_related("profile__team", "profile__department")
    if query:
        queryset = queryset.filter(Q(email__icontains=query) | Q(first_name__icontains=query) |
                                   Q(last_name__icontains=query) | Q(employee_id__icontains=query))
    queryset = queryset.annotate(
        challenge_count=Count("challenge_enrollments", distinct=True),
        completed_count=Count("challenge_enrollments__progress", filter=Q(
            challenge_enrollments__progress__is_complete=True), distinct=True),
        checkin_count=Count("wellness_enrollments__check_ins", distinct=True),
        last_checkin=Max("wellness_enrollments__check_ins__created_at"),
        recent_checkins=Count("wellness_enrollments__check_ins", filter=Q(
            wellness_enrollments__check_ins__created_at__gte=recent), distinct=True),
        recent_evaluations=Count("challenge_enrollments__progress", filter=Q(
            challenge_enrollments__progress__evaluated_at__gte=recent,
            challenge_enrollments__progress__has_step_data=True), distinct=True),
    ).order_by("email")
    page = Paginator(queryset, 20).get_page(request.GET.get("page"))
    page.object_list = list(page.object_list)
    ids = [employee.pk for employee in page.object_list]
    month_start, _ = step_period("month")
    month_steps = step_totals(ids, month_start, timezone.localdate())
    points = points_totals(ids)
    for employee in page.object_list:
        employee.month_steps = month_steps.get(employee.pk, {}).get("steps")
        employee.month_step_days = month_steps.get(employee.pk, {}).get("days", 0)
        employee.net_points = points.get(employee.pk, 0)
    return render(request, "portal/employees.html", page_context("Employees", "employees", page=page, query=query))


@staff_only
@never_cache
def employee_detail(request, pk):
    employee = get_object_or_404(User.objects.select_related("profile__team", "profile__department"),
                                 pk=pk, role=Role.EMPLOYEE)
    recent = timezone.now() - timedelta(days=7)
    challenge_count = ChallengeEnrollment.objects.filter(user=employee).count()
    completed_count = ChallengeProgress.objects.filter(enrollment__user=employee, is_complete=True).count()
    program_count = ProgramEnrollment.objects.filter(user=employee).count()
    checkin_count = ProgramCheckIn.objects.filter(enrollment__user=employee, created_at__gte=recent).count()
    last_sync = FitnessSource.objects.filter(user=employee).aggregate(value=Max("last_successful_sync_at"))["value"]
    today = timezone.localdate()
    month_value = request.GET.get("month", "")
    try:
        month_start = date.fromisoformat(f"{month_value}-01") if month_value else today.replace(day=1)
    except ValueError:
        month_start = today.replace(day=1)
    if month_start > today.replace(day=1):
        month_start = today.replace(day=1)
    next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    month_end = next_month - timedelta(days=1)
    account = FitnessAccount.objects.filter(user=employee).first()
    activity = DailyActivity.objects.filter(user=employee, source_id=account.active_source_id,
        local_date__range=(month_start, month_end)).order_by("-local_date") if account and account.active_source_id else DailyActivity.objects.none()
    activity_days = list(activity)
    summary = {}
    for period in ("today", "week", "month", "all"):
        start, end = step_period(period, today)
        summary[period] = step_totals([employee.pk], start, end).get(employee.pk)
    challenge_rows = []
    challenge_points = {row["enrollment_id"]: row["total"] or 0 for row in
        PointsLedger.objects.filter(enrollment__user=employee).values("enrollment_id").annotate(total=Sum("delta"))}
    for enrollment in ChallengeEnrollment.objects.filter(user=employee).select_related("challenge", "progress").order_by("-joined_at"):
        progress = getattr(enrollment, "progress", None)
        challenge_rows.append({"challenge": enrollment.challenge, "joined_at": enrollment.joined_at,
            "status": "Complete" if progress and progress.is_complete else
                "Ended" if enrollment.challenge.end_date < today else
                "Upcoming" if enrollment.challenge.start_date > today else "In progress",
            "points": challenge_points.get(enrollment.pk, 0),
            "steps": progress.verified_steps if progress and progress.has_step_data else None,
            "running_minutes": progress.verified_running_minutes if progress else None})
    program_rows = ProgramEnrollment.objects.filter(user=employee).select_related("program").annotate(
        checkin_total=Count("check_ins")).order_by("-joined_at")
    points = points_totals([employee.pk]).get(employee.pk, 0)
    hydration_month = HydrationLog.objects.filter(user=employee,
        local_date__range=(month_start, month_end)).aggregate(total_ml=Sum("volume_ml"), entries=Count("id"))
    rankings = {kind: next((row["rank"] for row in staff_rankings(kind) if row["user"].pk == employee.pk), None)
                for kind in ("points", "steps", "challenges")}
    return render(request, "portal/employee_detail.html", page_context(employee.full_name, "employees",
        employee=employee, challenge_count=challenge_count, completed_count=completed_count,
        program_count=program_count, checkin_count=checkin_count, last_sync=last_sync,
        step_summary=summary, activity_days=activity_days, challenge_rows=challenge_rows,
        program_rows=program_rows, points=points, rankings=rankings,
        month_start=month_start, previous_month=(month_start - timedelta(days=1)).strftime("%Y-%m"),
        next_month=next_month.strftime("%Y-%m") if next_month <= today else None,
        hydration_month=hydration_month, monthly_meals=Meal.objects.filter(user=employee,
            local_date__range=(month_start, month_end)).count(),
        community_post_count=CommunityPost.objects.filter(author=employee).count(),
        meal_count=Meal.objects.filter(user=employee).count()))


@staff_only
def employee_form(request, pk=None):
    employee = get_object_or_404(User, pk=pk, role=Role.EMPLOYEE) if pk else None
    form = EmployeeForm(request.POST or None, instance=employee)
    if request.method == "POST" and form.is_valid():
        saved = form.save()
        messages.success(request, "Employee saved.")
        return redirect("portal:employee_detail", pk=saved.pk)
    return render(request, "portal/form.html", page_context(
        "Edit employee" if employee else "Add employee", "employees", form=form,
        cancel_url=reverse("portal:employees"),
        help_text="Portal staff can create and edit employee accounts only. Roles and staff access cannot be changed here."))


SECTION_CONFIG = {
    "departments": (Department, DepartmentForm, "Departments", "department_new", "department_edit"),
    "teams": (Team, TeamForm, "Teams", "team_new", "team_edit"),
    "challenges": (Challenge, ChallengeForm, "Challenges", "challenge_new", "challenge_edit"),
    "programs": (WellnessProgram, WellnessProgramForm, "Wellness programs", "program_new", "program_edit"),
}


@staff_only
def records(request, section):
    model, _, title, new_name, edit_name = SECTION_CONFIG[section]
    if section == "departments":
        queryset = model.objects.annotate(member_count=Count("employees", distinct=True)).order_by("name")
        headers = ["Department", "Employees"]
        make_cells = lambda obj: [obj.name, obj.member_count]
    elif section == "teams":
        queryset = model.objects.select_related("department", "manager").annotate(
            member_count=Count("members", distinct=True)).order_by("name")
        headers = ["Team", "Department", "Manager", "Employees"]
        make_cells = lambda obj: [obj.name, obj.department or "—", obj.manager or "—", obj.member_count]
    elif section == "challenges":
        queryset = model.objects.annotate(enrollment_count=Count("enrollments", distinct=True)).order_by("-start_date", "-pk")
        headers = ["Challenge", "Dates", "Target", "Participants", "Published"]
        make_cells = lambda obj: [obj.title, f"{obj.start_date} to {obj.end_date}",
                                  f"{obj.target_running_minutes} running min" if obj.kind == Challenge.Kind.RUNNING else f"{obj.target_steps} steps",
                                  obj.enrollment_count, "Yes" if obj.is_published else "Draft"]
    else:
        queryset = model.objects.annotate(enrollment_count=Count("enrollments", distinct=True)).order_by("-start_date", "-pk")
        headers = ["Program", "Dates", "Focus", "Participants", "Published"]
        make_cells = lambda obj: [obj.title, f"{obj.start_date} to {obj.end_date}", obj.get_focus_goal_display(),
                                  obj.enrollment_count, "Yes" if obj.is_published else "Draft"]
    page = Paginator(queryset, 20).get_page(request.GET.get("page"))
    rows = [{"cells": make_cells(obj), "edit_url": reverse(f"portal:{edit_name}", args=[obj.pk])} for obj in page]
    return render(request, "portal/records.html", page_context(title, section, page=page, rows=rows,
        headers=headers, new_url=reverse(f"portal:{new_name}")))


@staff_only
def record_form(request, section, pk=None):
    model, form_class, title, _, list_name = SECTION_CONFIG[section]
    instance = get_object_or_404(model, pk=pk) if pk else None
    form = form_class(request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            saved = form.save(commit=False)
            saved.full_clean()
            saved.save()
            if section == "challenges":
                for user_id in ChallengeEnrollment.objects.filter(challenge=saved).values_list("user_id", flat=True):
                    reconcile_user(User.objects.get(pk=user_id))
        messages.success(request, f"{title[:-1] if title.endswith('s') else title} saved.")
        return redirect(f"portal:{section}")
    return render(request, "portal/form.html", page_context(
        f"Edit {title.lower().rstrip('s')}" if instance else f"Add {title.lower().rstrip('s')}",
        section, form=form, cancel_url=reverse(f"portal:{section}"),
        help_text="Published items remain visible in the app. Fields that affect an existing enrollment are locked."))


@staff_only
def posts(request):
    page = Paginator(CommunityPost.objects.select_related("author", "team").order_by("-created_at"), 20).get_page(
        request.GET.get("page"))
    return render(request, "portal/posts.html", page_context("Community posts", "posts", page=page))


@staff_only
def post_detail(request, pk):
    post = get_object_or_404(CommunityPost.objects.select_related("author", "team"), pk=pk)
    return render(request, "portal/post_detail.html", page_context("Post review", "posts", post=post))


@staff_only
def post_photo(request, pk):
    post = get_object_or_404(CommunityPost, pk=pk)
    if not post.image:
        raise Http404
    response = FileResponse(post.image.open("rb"), content_type="image/jpeg")
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@staff_only
@require_POST
def hide_post(request, pk):
    post = get_object_or_404(CommunityPost, pk=pk)
    if post.status != CommunityPost.Status.HIDDEN:
        post.status = CommunityPost.Status.HIDDEN
        post.save(update_fields=["status", "updated_at"])
    messages.success(request, "Post hidden from the app feed.")
    return redirect("portal:post_detail", pk=post.pk)


@staff_only
def reports(request):
    state = request.GET.get("status", CommunityReport.Status.OPEN)
    if state not in CommunityReport.Status.values:
        state = CommunityReport.Status.OPEN
    page = Paginator(CommunityReport.objects.filter(status=state).select_related(
        "post", "post__author", "reporter").order_by("-created_at"), 20).get_page(request.GET.get("page"))
    return render(request, "portal/reports.html", page_context("Community reports", "reports", page=page, state=state))


@staff_only
def report_detail(request, pk):
    report = get_object_or_404(CommunityReport.objects.select_related("post", "post__author", "reporter"), pk=pk)
    return render(request, "portal/report_detail.html", page_context("Report review", "reports", report=report))


@staff_only
@require_POST
def resolve_report(request, pk):
    report = get_object_or_404(CommunityReport, pk=pk)
    report.status = CommunityReport.Status.RESOLVED
    report.reviewed_at = timezone.now()
    report.save(update_fields=["status", "reviewed_at"])
    messages.success(request, "Report marked resolved. The post visibility was not changed.")
    return redirect("portal:report_detail", pk=pk)


@staff_only
@require_POST
def hide_reported_post(request, pk):
    with transaction.atomic():
        report = get_object_or_404(CommunityReport.objects.select_related("post"), pk=pk)
        report.post.status = CommunityPost.Status.HIDDEN
        report.post.save(update_fields=["status", "updated_at"])
        report.status = CommunityReport.Status.RESOLVED
        report.reviewed_at = timezone.now()
        report.save(update_fields=["status", "reviewed_at"])
    messages.success(request, "Post hidden and report resolved.")
    return redirect("portal:report_detail", pk=pk)
