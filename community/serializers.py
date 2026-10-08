from rest_framework import serializers

from .models import CommunityPost


class PostWriteSerializer(serializers.Serializer):
    body = serializers.CharField(max_length=2000, allow_blank=False, trim_whitespace=True)
    hashtags = serializers.ListField(child=serializers.CharField(max_length=30), required=False, max_length=10)
    visibility = serializers.ChoiceField(choices=CommunityPost.Visibility.choices, required=False,
                                         default=CommunityPost.Visibility.TEAM)
    client_request_id = serializers.UUIDField(required=False)

    def validate_body(self, value):
        if not value.strip():
            raise serializers.ValidationError("Post text is required.")
        return value.strip()

    def validate_hashtags(self, value):
        tags = []
        for raw in value:
            tag = raw.strip().lstrip("#")
            if not tag or not tag.replace("_", "").isalnum():
                raise serializers.ValidationError("Hashtags may contain letters, numbers and underscores.")
            normalized = "#" + tag
            if normalized not in tags:
                tags.append(normalized)
        return tags


class PhotoPostWriteSerializer(PostWriteSerializer):
    body = serializers.CharField(max_length=2000, allow_blank=True, required=False, default="", trim_whitespace=True)
    image = serializers.FileField(required=True)

    def validate_body(self, value):
        return value.strip()


class CommentWriteSerializer(serializers.Serializer):
    body = serializers.CharField(max_length=500, allow_blank=False, trim_whitespace=True)


class ReactionSerializer(serializers.Serializer):
    active = serializers.BooleanField()


class ReportSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=500, allow_blank=False, trim_whitespace=True)
