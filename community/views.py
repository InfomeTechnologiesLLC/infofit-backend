import hashlib
import io
import uuid

from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.http import FileResponse
from django.utils import timezone
from PIL import Image, ImageOps, UnidentifiedImageError
from rest_framework import status
from rest_framework.exceptions import APIException, NotFound, PermissionDenied, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import serializers
from rest_framework.throttling import ScopedRateThrottle

import logging

from django.conf import settings

from accounts.models import ADMIN_ROLES, EmployeeProfile
from . import moderation
from .models import CommunityComment, CommunityPost, CommunityReaction, CommunityReport
from .serializers import CommentWriteSerializer, PhotoPostWriteSerializer, PostWriteSerializer, ReactionSerializer, ReportSerializer
from notifications.models import Notification
from notifications.services import create_notification


_log = logging.getLogger("infome.community")


def team_id_for(user):
    return EmployeeProfile.objects.filter(user=user).values_list("team_id", flat=True).first()


class PhotoStorageUnavailable(APIException):
    status_code = 503
    default_detail = "The server couldn't save the photo right now. Please try again later."
    default_code = "photo_storage_unavailable"


class RequestConflict(APIException):
    status_code = 409
    default_detail = "This request ID was already used for a different post."


def visible_posts(user):
    """Published posts the employee may see, plus their own posts that are waiting for review."""
    team_id = team_id_for(user)
    allowed = Q(visibility=CommunityPost.Visibility.COMPANY) | Q(author=user)
    if team_id:
        allowed |= Q(visibility=CommunityPost.Visibility.TEAM, team_id=team_id)
    return CommunityPost.objects.filter(
        Q(status=CommunityPost.Status.PUBLISHED) & allowed |
        Q(status=CommunityPost.Status.REVIEW, author=user))


def screened_fields(body):
    """Status fields for a new post after the automatic check."""
    verdict, reason = moderation.screen(body)
    if verdict == "review":
        return {"status": CommunityPost.Status.REVIEW, "moderation_note": reason or "Held by the automatic check."}
    return {}


def post_or_404(user, pk):
    post = visible_posts(user).select_related("author", "team", "author__profile").filter(pk=pk).first()
    if not post:
        raise NotFound("Post not found.")
    return post


def post_queryset(user):
    return visible_posts(user).select_related("author", "team", "author__profile").annotate(
        likes_count=Count("reactions", filter=Q(reactions__kind=CommunityReaction.Kind.LIKE), distinct=True),
        celebrate_count=Count("reactions", filter=Q(reactions__kind=CommunityReaction.Kind.CELEBRATE), distinct=True),
        fire_count=Count("reactions", filter=Q(reactions__kind=CommunityReaction.Kind.FIRE), distinct=True),
        comments_count=Count("comments", filter=Q(comments__status=CommunityPost.Status.PUBLISHED), distinct=True),
    )


def post_payload(post, user, user_kinds=None):
    kinds = user_kinds if user_kinds is not None else set(CommunityReaction.objects.filter(
        post=post, user=user).values_list("kind", flat=True))
    profile = getattr(post.author, "profile", None)
    return {
        "id": post.id, "author_name": post.author.full_name,
        "author_role": profile.designation if profile else "",
        "team_name": post.team.name if post.team_id else None,
        "visibility": post.visibility, "body": post.body, "hashtags": post.hashtags,
        "has_image": bool(post.image),
        "created_at": post.created_at,
        "likes_count": post.likes_count, "celebrate_count": post.celebrate_count,
        "fire_count": post.fire_count, "comments_count": post.comments_count,
        "user_liked": CommunityReaction.Kind.LIKE in kinds,
        "user_celebrated": CommunityReaction.Kind.CELEBRATE in kinds,
        "user_fired": CommunityReaction.Kind.FIRE in kinds,
        "can_delete": post.author_id == user.id or user.role in ADMIN_ROLES or user.is_superuser,
        "status": post.status,
    }


def fresh_post_payload(pk, user):
    post = post_queryset(user).get(pk=pk)
    return post_payload(post, user)


class FeedPagination(PageNumberPagination):
    page_size = 20


class PostListView(APIView):
    def get(self, request):
        queryset = post_queryset(request.user).order_by("-created_at", "-pk")
        paginator = FeedPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        ids = [post.pk for post in page]
        own = CommunityReaction.objects.filter(user=request.user, post_id__in=ids).values_list("post_id", "kind")
        kinds_by_post = {pk: set() for pk in ids}
        for pk, kind in own:
            kinds_by_post[pk].add(kind)
        return paginator.get_paginated_response([post_payload(post, request.user, kinds_by_post[post.pk]) for post in page])

    def post(self, request):
        serializer = PostWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        request_id = data.get("client_request_id")
        if request_id:
            existing = CommunityPost.objects.filter(author=request.user, client_request_id=request_id).first()
            if existing:
                if existing.status == CommunityPost.Status.HIDDEN:
                    raise RequestConflict("This post is no longer visible.")
                if (existing.image or existing.body != data["body"] or existing.hashtags != data.get("hashtags", [])
                        or existing.visibility != data["visibility"]):
                    raise RequestConflict()
                return Response(fresh_post_payload(existing.pk, request.user))
        team_id = team_id_for(request.user)
        if data["visibility"] == CommunityPost.Visibility.TEAM and not team_id:
            raise ValidationError({"visibility": "Join a team before posting to a team feed."})
        fields = {"team_id": team_id, "body": data["body"],
                  "hashtags": data.get("hashtags", []), "visibility": data["visibility"]}
        screened = screened_fields(data["body"])
        with transaction.atomic():
            if request_id:
                post, created = CommunityPost.objects.get_or_create(
                    author=request.user, client_request_id=request_id, defaults={**fields, **screened})
                if not created and (post.image or any(getattr(post, key) != value for key, value in fields.items() if key != "team_id")):
                    raise RequestConflict()
            else:
                post = CommunityPost.objects.create(author=request.user, **fields, **screened)
                created = True
        return Response(fresh_post_payload(post.pk, request.user), status=status.HTTP_201_CREATED if created else 200)


# Pillow 9.1+ moved the filter names into Image.Resampling; older Pillow (the only kind Python 3.6 servers can
# install, up to 8.4) only has Image.LANCZOS. This works on both.
LANCZOS = getattr(getattr(Image, "Resampling", Image), "LANCZOS")


def prepared_photo(upload):
    if upload.size > 5 * 1024 * 1024:
        raise ValidationError({"image": "Choose a photo smaller than 5 MB."})
    try:
        raw = upload.read()
        with Image.open(io.BytesIO(raw)) as original:
            if original.format not in ("JPEG", "PNG", "WEBP") or original.width * original.height > 25_000_000:
                raise ValueError("Unsupported or oversized image")
            original.verify()
        with Image.open(io.BytesIO(raw)) as original:
            image = ImageOps.exif_transpose(original)
            image.thumbnail((1600, 1600), LANCZOS)
            output = io.BytesIO()
            image.convert("RGB").save(output, format="JPEG", quality=82, optimize=True)
            content = output.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ValidationError({"image": "Choose a valid JPEG, PNG, or WebP photo."}) from exc
    return content, hashlib.sha256(content).hexdigest()


class PhotoPostView(APIView):
    def post(self, request):
        serializer = PhotoPostWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        photo, digest = prepared_photo(data["image"])
        team_id = team_id_for(request.user)
        if data["visibility"] == CommunityPost.Visibility.TEAM and not team_id:
            raise ValidationError({"visibility": "Join a team before posting to a team feed."})
        request_id = data.get("client_request_id")
        fields = {"team_id": team_id, "body": data["body"],
                  "hashtags": data.get("hashtags", []), "visibility": data["visibility"],
                  "image_sha256": digest}
        existing = CommunityPost.objects.filter(author=request.user, client_request_id=request_id).first() if request_id else None
        if existing:
            if not self.matches(existing, fields):
                raise RequestConflict()
            return Response(fresh_post_payload(existing.pk, request.user))
        post = CommunityPost(author=request.user, client_request_id=request_id, **fields, **screened_fields(data["body"]))
        try:
            post.image.save(f"{uuid.uuid4().hex}.jpg", ContentFile(photo), save=False)
        except OSError as exc:
            # Usually MEDIA_ROOT is missing, read-only or full on the server.
            _log.exception("Could not store community photo in %s", settings.MEDIA_ROOT)
            raise PhotoStorageUnavailable() from exc
        try:
            with transaction.atomic():
                post.save()
        except IntegrityError:
            post.image.storage.delete(post.image.name)
            existing = CommunityPost.objects.filter(author=request.user, client_request_id=request_id).first() if request_id else None
            if not existing or not self.matches(existing, fields):
                raise RequestConflict()
            return Response(fresh_post_payload(existing.pk, request.user))
        except Exception:
            post.image.storage.delete(post.image.name)
            raise
        return Response(fresh_post_payload(post.pk, request.user), status=status.HTTP_201_CREATED)

    @staticmethod
    def matches(post, fields):
        return post.status != CommunityPost.Status.HIDDEN and bool(post.image) and all(
            getattr(post, key) == value for key, value in fields.items() if key != "team_id"
        )


class PostPhotoView(APIView):
    def get(self, request, pk):
        post = post_or_404(request.user, pk)
        if not post.image:
            raise NotFound("Photo not found.")
        response = FileResponse(post.image.open("rb"), content_type="image/jpeg")
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response


class PostDetailView(APIView):
    def get(self, request, pk):
        post_or_404(request.user, pk)
        return Response(fresh_post_payload(pk, request.user))

    def patch(self, request, pk):
        post = post_or_404(request.user, pk)
        if post.author_id != request.user.id:
            raise PermissionDenied("Only the author can edit a post.")
        serializer = PostWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        visibility = data.get("visibility", post.visibility)
        if visibility == CommunityPost.Visibility.TEAM and not post.team_id:
            raise ValidationError({"visibility": "This post has no team."})
        for field in ("body", "hashtags", "visibility"):
            if field in data:
                setattr(post, field, data[field])
        post.save()
        return Response(fresh_post_payload(pk, request.user))

    def delete(self, request, pk):
        post = post_or_404(request.user, pk)
        if post.author_id != request.user.id and request.user.role not in ADMIN_ROLES and not request.user.is_superuser:
            raise PermissionDenied("Only the author or an administrator can remove a post.")
        post.status = CommunityPost.Status.HIDDEN
        post.save(update_fields=["status", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)


def comment_payload(comment):
    return {"id": comment.id, "author_name": comment.author.full_name,
            "body": comment.body, "created_at": comment.created_at}


class CommentListView(APIView):
    def get(self, request, pk):
        post_or_404(request.user, pk)
        comments = CommunityComment.objects.filter(post_id=pk, status=CommunityPost.Status.PUBLISHED).select_related("author").order_by("created_at", "pk")[:100]
        return Response([comment_payload(comment) for comment in comments])

    def post(self, request, pk):
        post = post_or_404(request.user, pk)
        serializer = CommentWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            comment = CommunityComment.objects.create(post_id=pk, author=request.user, body=serializer.validated_data["body"])
            if post.author_id != request.user.id:
                create_notification(
                    post.author, Notification.Kind.COMMUNITY_REPLY, "New reply",
                    "Someone replied to your community post.", f"community:comment:{comment.pk}",
                )
        return Response(comment_payload(comment), status=status.HTTP_201_CREATED)


class CommentDetailView(APIView):
    def delete(self, request, pk, comment_id):
        post_or_404(request.user, pk)
        comment = CommunityComment.objects.filter(pk=comment_id, post_id=pk, status=CommunityPost.Status.PUBLISHED).first()
        if not comment:
            raise NotFound("Comment not found.")
        if comment.author_id != request.user.id and request.user.role not in ADMIN_ROLES and not request.user.is_superuser:
            raise PermissionDenied("Only the author or an administrator can remove a comment.")
        comment.status = CommunityPost.Status.HIDDEN
        comment.save(update_fields=["status"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class ReactionView(APIView):
    def put(self, request, pk, kind):
        post_or_404(request.user, pk)
        if kind not in CommunityReaction.Kind.values:
            raise ValidationError({"kind": "Unsupported reaction."})
        serializer = ReactionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if serializer.validated_data["active"]:
            CommunityReaction.objects.get_or_create(post_id=pk, user=request.user, kind=kind)
        else:
            CommunityReaction.objects.filter(post_id=pk, user=request.user, kind=kind).delete()
        return Response(fresh_post_payload(pk, request.user))


class ReportView(APIView):
    def post(self, request, pk):
        post_or_404(request.user, pk)
        serializer = ReportSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        report, created = CommunityReport.objects.get_or_create(
            post_id=pk, reporter=request.user,
            defaults={"reason": serializer.validated_data["reason"]},
        )
        return Response({"reported": True}, status=status.HTTP_201_CREATED if created else 200)


class AssistSerializer(serializers.Serializer):
    body = serializers.CharField(max_length=2000, trim_whitespace=True)


class PostAssistView(APIView):
    """Caption and hashtag suggestions for a draft. Nothing is posted; the employee chooses what to use."""
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "ai"

    def post(self, request):
        serializer = AssistSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(moderation.assist(serializer.validated_data["body"]))
