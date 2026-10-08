from django.conf import settings
from django.db import models


class CommunityPost(models.Model):
    class Visibility(models.TextChoices):
        TEAM = "team", "Team"
        COMPANY = "company", "Company"

    class Status(models.TextChoices):
        PUBLISHED = "published", "Published"
        HIDDEN = "hidden", "Hidden"

    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="community_posts")
    team = models.ForeignKey("accounts.Team", null=True, blank=True, on_delete=models.SET_NULL)
    visibility = models.CharField(max_length=10, choices=Visibility.choices, default=Visibility.TEAM)
    body = models.TextField(max_length=2000)
    image = models.ImageField(upload_to="community/posts/", null=True, blank=True)
    image_sha256 = models.CharField(max_length=64, blank=True, default="")
    client_request_id = models.UUIDField(null=True, blank=True)
    hashtags = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PUBLISHED, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["author", "client_request_id"], name="uniq_community_post_request")]
        indexes = [models.Index(fields=["status", "created_at"], name="community_feed_idx"),
                   models.Index(fields=["team", "status", "created_at"], name="community_team_feed_idx")]


class CommunityComment(models.Model):
    post = models.ForeignKey(CommunityPost, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    body = models.CharField(max_length=500)
    status = models.CharField(max_length=12, choices=CommunityPost.Status.choices,
                              default=CommunityPost.Status.PUBLISHED, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["post", "status", "created_at"], name="community_comment_idx")]


class CommunityReaction(models.Model):
    class Kind(models.TextChoices):
        LIKE = "like", "Like"
        CELEBRATE = "celebrate", "Celebrate"
        FIRE = "fire", "Fire"

    post = models.ForeignKey(CommunityPost, on_delete=models.CASCADE, related_name="reactions")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["post", "user", "kind"], name="uniq_community_reaction")]


class CommunityReport(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        RESOLVED = "resolved", "Resolved"

    post = models.ForeignKey(CommunityPost, on_delete=models.CASCADE, related_name="reports")
    reporter = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    reason = models.CharField(max_length=500)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["post", "reporter"], name="uniq_community_report")]
