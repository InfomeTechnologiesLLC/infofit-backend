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

from .forms import (CHALLENGE_KIND_HELP, ChallengeForm, DepartmentForm, EmployeeForm, PortalLoginForm,
                    TeamForm, WellnessProgramForm)
from .insights import points_totals, staff_rankings, step_period, step_totals



def _iso_date(value):
    """Parse YYYY-MM-DD (date.fromisoformat needs Python 3.7+)."""
    from datetime import datetime as _dt
    return _dt.strptime(value, "%Y-%m-%d").date()

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
    from . import dashboard as figures
    today = timezone.localdate()
    week_start, _ = step_period("week")
    return render(request, "portal/dashboard.html", page_context("Overview", "dashboard",
        today=today, kpis=figures.kpis(today), activity=figures.activity_series(today),
        signups=figures.signup_series(today), departments=figures.department_participation(today),
        live=figures.live_challenges(today), attention=figures.attention(today),
        top_staff=staff_rankings()[:5], top_teams=figures.top_teams(),
        top_steps=[row for row in staff_rankings("steps", "week") if row["step_days"]][:5],
        recent_signups=figures.recent_signups(), habits=figures.today_habits(today),
        recent_reports=CommunityReport.objects.filter(status=CommunityReport.Status.OPEN).select_related(
            "post", "reporter").order_by("-created_at")[:5]))


@staff_only
@never_cache
def challenge_detail(request, pk):
    from . import dashboard as figures
    challenge = get_object_or_404(Challenge, pk=pk)
    today = timezone.localdate()
    enrollments = list(ChallengeEnrollment.objects.filter(challenge=challenge).select_related(
        "user", "user__profile__team", "progress").order_by("joined_at"))
    points = {row["enrollment_id"]: row["total"] or 0 for row in PointsLedger.objects.filter(
        enrollment__challenge=challenge).values("enrollment_id").annotate(total=Sum("delta"))}
    syncs = figures.last_sync_by_user([e.user_id for e in enrollments])
    rows, buckets = [], [0, 0, 0, 0, 0]
    for e in enrollments:
        progress = getattr(e, "progress", None)
        value = progress.progress_value if progress and progress.has_step_data else None
        percent = min(100, round(100 * value / challenge.target)) if value is not None and challenge.target else 0
        done = bool(progress and progress.is_complete)
        buckets[4 if done else min(3, percent // 25)] += 1
        rows.append({"enrollment": e, "label": challenge.progress_label(progress) or "No recorded data yet",
                     "percent": 100 if done else percent, "done": done, "points": points.get(e.pk, 0),
                     "flagged": progress.flagged_days if progress else 0, "last_sync": syncs.get(e.user_id),
                     "value": value or 0})
    rows.sort(key=lambda r: (not r["done"], -r["value"]))
    completed = sum(1 for r in rows if r["done"])
    with_data = [r for r in rows if r["value"]]
    if challenge.start_date > today:
        status = f"Starts {challenge.start_date:%d %b %Y}"
    elif challenge.end_date < today:
        status = "Ended"
    else:
        left = (challenge.end_date - today).days
        status = "Last day" if left == 0 else f"{left} day{'s' if left != 1 else ''} left"
    return render(request, "portal/challenge_detail.html", page_context(challenge.title, "challenges",
        challenge=challenge, rows=rows, status=status, icon=figures.KIND_ICONS.get(challenge.kind, "bi-flag"),
        stats={"joined": len(rows), "completed": completed,
               "percent": round(100 * completed / len(rows)) if rows else 0,
               "flagged": sum(1 for r in rows if r["flagged"]),
               "average": (sum(r["value"] for r in with_data) / len(with_data)) if with_data else None},
        buckets=buckets))


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
    quick = request.GET.get("filter", "")
    if quick == "no_team":
        queryset = queryset.filter(Q(profile__team__isnull=True) | Q(profile__isnull=True), is_active=True)
    elif quick == "not_synced":
        synced = FitnessSource.objects.filter(last_successful_sync_at__gte=recent).values_list("user_id", flat=True)
        queryset = queryset.filter(is_active=True).exclude(pk__in=synced)
    elif quick == "new":
        queryset = queryset.filter(date_joined__gte=recent)
    else:
        quick = ""
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
    return render(request, "portal/employees.html", page_context("Employees", "employees", page=page, query=query,
                                                                 quick=quick))


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
        month_start = _iso_date(f"{month_value}-01") if month_value else today.replace(day=1)
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
            "running_minutes": progress.verified_running_minutes if progress else None,
            "label": enrollment.challenge.progress_label(progress), "flagged": progress.flagged_days if progress else 0})
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
        return challenge_list(request)
    else:
        # Personal plans belong to one employee and are never listed or edited here.
        queryset = model.objects.filter(owner__isnull=True).annotate(
            enrollment_count=Count("enrollments", distinct=True)).order_by("-start_date", "-pk")
        headers = ["Program", "Dates", "Focus", "Participants", "Published"]
        make_cells = lambda obj: [obj.title, f"{obj.start_date} to {obj.end_date}", obj.get_focus_goal_display(),
                                  obj.enrollment_count, "Yes" if obj.is_published else "Draft"]
    page = Paginator(queryset, 20).get_page(request.GET.get("page"))
    rows = [{"cells": make_cells(obj), "edit_url": reverse(f"portal:{edit_name}", args=[obj.pk])} for obj in page]
    return render(request, "portal/records.html", page_context(title, section, page=page, rows=rows,
        headers=headers, new_url=reverse(f"portal:{new_name}")))


def challenge_list(request):
    from . import dashboard as figures
    today = timezone.localdate()
    state = request.GET.get("state", "")
    queryset = Challenge.objects.annotate(
        joined=Count("enrollments", distinct=True),
        completed=Count("enrollments", filter=Q(enrollments__progress__is_complete=True), distinct=True),
    ).order_by("-start_date", "-pk")
    if state == "live":
        queryset = queryset.filter(is_published=True, start_date__lte=today, end_date__gte=today)
    elif state == "upcoming":
        queryset = queryset.filter(start_date__gt=today)
    elif state == "ended":
        queryset = queryset.filter(end_date__lt=today)
    elif state == "draft":
        queryset = queryset.filter(is_published=False)
    else:
        state = ""
    page = Paginator(queryset, 20).get_page(request.GET.get("page"))
    for c in page:
        c.icon = figures.KIND_ICONS.get(c.kind, "bi-flag")
        c.percent = round(100 * c.completed / c.joined) if c.joined else 0
        c.phase = ("draft" if not c.is_published else "upcoming" if c.start_date > today else
                   "ended" if c.end_date < today else "live")
    return render(request, "portal/challenges.html", page_context("Challenges", "challenges", page=page, state=state,
                                                                  today=today))


@staff_only
def record_form(request, section, pk=None):
    model, form_class, title, _, list_name = SECTION_CONFIG[section]
    base = model.objects.filter(owner__isnull=True) if section == "programs" else model.objects.all()
    instance = get_object_or_404(base, pk=pk) if pk else None
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
        if section == "challenges":
            return redirect("portal:challenge_detail", pk=saved.pk)
        return redirect(f"portal:{section}")
    return render(request, "portal/form.html", page_context(
        f"Edit {title.lower().rstrip('s')}" if instance else f"Add {title.lower().rstrip('s')}",
        section, form=form, cancel_url=reverse(f"portal:{section}"),
        draft_kind={"challenges": "challenge", "programs": "program"}.get(section),
        kind_help=CHALLENGE_KIND_HELP if section == "challenges" else None,
        help_text="Published items remain visible in the app. Fields that affect an existing enrollment are locked."))


@staff_only
def posts(request):
    state = request.GET.get("status", "")
    queryset = CommunityPost.objects.select_related("author", "team").order_by("-created_at")
    if state in CommunityPost.Status.values:
        queryset = queryset.filter(status=state)
    page = Paginator(queryset, 20).get_page(request.GET.get("page"))
    waiting = CommunityPost.objects.filter(status=CommunityPost.Status.REVIEW).count()
    return render(request, "portal/posts.html", page_context("Community posts", "posts", page=page, state=state,
                                                             waiting=waiting))


@staff_only
@require_POST
def approve_post(request, pk):
    post = get_object_or_404(CommunityPost, pk=pk)
    if post.status != CommunityPost.Status.PUBLISHED:
        post.status = CommunityPost.Status.PUBLISHED
        post.save(update_fields=["status", "updated_at"])
    messages.success(request, "Post published to the app feed.")
    return redirect("portal:post_detail", pk=post.pk)


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



# ---- AI help for staff ----------------------------------------------------------------------------

@staff_only
@never_cache
def insights(request):
    from ai_service.provider import AiUnavailable

    from . import ai
    question = (request.POST.get("question") or "").strip()[:500] if request.method == "POST" else ""
    answer = error = None
    dataset = ai.insight_dataset()
    if question:
        try:
            answer = ai.ask(question, dataset)
        except AiUnavailable:
            error = "The AI assistant is unavailable right now. The figures below are still current."
    return render(request, "portal/insights.html", page_context(
        "Ask about participation", "insights", question=question, answer=answer, error=error,
        dataset=dataset, suggestions=ai.SUGGESTED_QUESTIONS))


@staff_only
@require_POST
def ai_draft(request):
    from django.http import JsonResponse

    from ai_service.provider import AiUnavailable

    from . import ai
    kind = request.POST.get("kind")
    brief = (request.POST.get("brief") or "").strip()
    if kind not in ai.DRAFT_SYSTEMS or not 3 <= len(brief) <= 600:
        return JsonResponse({"error": "Describe what you want in a few words (up to 600 characters)."}, status=400)
    try:
        result = ai.draft(kind, brief)
    except AiUnavailable:
        result = None
    if not result:
        return JsonResponse({"error": "The AI assistant is unavailable right now. Please write it yourself."}, status=503)
    return JsonResponse({"draft": result})
