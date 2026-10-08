from django.contrib import admin
from django.db import transaction
from django.http import FileResponse, Http404
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html

from accounts.models import ADMIN_ROLES
from .models import CommunityComment, CommunityPost, CommunityReaction, CommunityReport


class CommunityAdminOnly:
    def has_module_permission(self, request):
        user = request.user
        return bool(user.is_authenticated and (user.is_superuser or getattr(user, "role", None) in ADMIN_ROLES))

    def has_view_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.action(description="Hide selected posts")
def hide_posts(modeladmin, request, queryset):
    queryset.update(status=CommunityPost.Status.HIDDEN, updated_at=timezone.now())


@admin.action(description="Hide selected comments")
def hide_comments(modeladmin, request, queryset):
    queryset.update(status=CommunityPost.Status.HIDDEN)


@admin.action(description="Resolve selected reports")
def resolve_reports(modeladmin, request, queryset):
    queryset.update(status=CommunityReport.Status.RESOLVED, reviewed_at=timezone.now())


@admin.action(description="Hide reported posts and resolve selected reports")
def hide_reported_posts(modeladmin, request, queryset):
    with transaction.atomic():
        post_ids = list(queryset.values_list("post_id", flat=True))
        CommunityPost.objects.filter(pk__in=post_ids).update(
            status=CommunityPost.Status.HIDDEN, updated_at=timezone.now()
        )
        queryset.update(status=CommunityReport.Status.RESOLVED, reviewed_at=timezone.now())


@admin.register(CommunityPost)
class CommunityPostAdmin(CommunityAdminOnly, admin.ModelAdmin):
    list_display = ("id", "author", "team", "visibility", "has_photo", "status", "created_at")
    list_filter = ("status", "visibility", "team")
    search_fields = ("body", "author__email")
    actions = [hide_posts]

    @admin.display(boolean=True, description="Photo")
    def has_photo(self, obj):
        return bool(obj.image)

    def get_urls(self):
        return [path("<int:post_id>/photo/", self.admin_site.admin_view(self.photo_view),
                     name="community_post_photo")] + super().get_urls()

    def photo_view(self, request, post_id):
        if not self.has_view_permission(request):
            raise Http404()
        post = CommunityPost.objects.filter(pk=post_id).first()
        if not post or not post.image:
            raise Http404()
        response = FileResponse(post.image.open("rb"), content_type="image/jpeg")
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response


@admin.register(CommunityComment)
class CommunityCommentAdmin(CommunityAdminOnly, admin.ModelAdmin):
    list_display = ("id", "post", "author", "status", "created_at")
    list_filter = ("status",)
    actions = [hide_comments]


@admin.register(CommunityReport)
class CommunityReportAdmin(CommunityAdminOnly, admin.ModelAdmin):
    list_display = ("id", "post_link", "post_excerpt", "post_photo", "post_author", "reason", "reporter", "status", "created_at")
    list_display_links = ("id",)
    list_select_related = ("post", "post__author", "reporter")
    list_filter = ("status",)
    search_fields = ("post__body", "post__author__email", "reason", "reporter__email")
    readonly_fields = ("post_link", "post_body", "post_photo", "post_author", "reason", "reporter", "status", "created_at", "reviewed_at")
    fields = ("post_link", "post_body", "post_photo", "post_author", "reason", "reporter", "status", "created_at", "reviewed_at")
    actions = [hide_reported_posts, resolve_reports]

    @admin.display(description="Post")
    def post_link(self, obj):
        if not obj.pk:
            return ""
        url = reverse("admin:community_communitypost_change", args=[obj.post_id])
        return format_html('<a href="{}">Post #{}</a>', url, obj.post_id)

    @admin.display(description="Post text")
    def post_excerpt(self, obj):
        body = obj.post.body
        return body[:100] + ("…" if len(body) > 100 else "") if body else ("[Photo]" if obj.post.image else "")

    @admin.display(description="Post photo")
    def post_photo(self, obj):
        if not obj.pk or not obj.post.image:
            return ""
        url = reverse("admin:community_post_photo", args=[obj.post_id])
        return format_html('<a href="{}" target="_blank" rel="noopener">View photo</a>', url)

    @admin.display(description="Full post text")
    def post_body(self, obj):
        return obj.post.body if obj.pk else ""

    @admin.display(description="Post author")
    def post_author(self, obj):
        return obj.post.author.email if obj.pk else ""


@admin.register(CommunityReaction)
class CommunityReactionAdmin(CommunityAdminOnly, admin.ModelAdmin):
    list_display = ("post", "user", "kind", "created_at")

    def has_change_permission(self, request, obj=None):
        return False
