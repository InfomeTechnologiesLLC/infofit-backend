from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Notification, PushDevice


def payload(item):
    return {"id": item.id, "kind": item.kind, "title": item.title, "body": item.body,
            "data": item.data or {}, "created_at": item.created_at, "read_at": item.read_at}


class NotificationPagination(PageNumberPagination):
    page_size = 20


class InboxView(APIView):
    def get(self, request):
        items = Notification.objects.filter(user=request.user).order_by("-created_at", "-pk")
        paginator = NotificationPagination()
        page = paginator.paginate_queryset(items, request, view=self)
        response = paginator.get_paginated_response([payload(item) for item in page])
        response.data["unread_count"] = Notification.objects.filter(user=request.user, read_at__isnull=True).count()
        return response


class ReadView(APIView):
    def post(self, request, pk):
        item = Notification.objects.filter(user=request.user, pk=pk).first()
        if item is None:
            raise NotFound("Notification not found.")
        if item.read_at is None:
            item.read_at = timezone.now()
            item.save(update_fields=["read_at"])
        return Response(payload(item))


class DeviceView(APIView):
    def post(self, request):
        token = request.data.get("token")
        if not isinstance(token, str) or not 20 <= len(token) <= 512 or any(c.isspace() for c in token):
            raise ValidationError({"token": "Provide a valid push registration token."})
        # A physical app installation can change employees. Delete the old registration and
        # its undelivered pushes before assigning this token to the new signed-in employee.
        with transaction.atomic():
            PushDevice.objects.filter(token=token).exclude(user=request.user).delete()
            device, created = PushDevice.objects.update_or_create(
                token=token, defaults={"user": request.user, "enabled": True}
            )
        return Response({"registered": True}, status=status.HTTP_201_CREATED if created else 200)

    def delete(self, request):
        token = request.data.get("token")
        if not isinstance(token, str):
            raise ValidationError({"token": "Provide a push registration token."})
        PushDevice.objects.filter(user=request.user, token=token).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
