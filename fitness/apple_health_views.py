import hashlib
import json

from rest_framework.parsers import JSONParser
from rest_framework.response import Response
from rest_framework.views import APIView

from .apple_health import AppleHealthAuthentication, ingest, issue_token, revoke_token
from .apple_health_serializers import AppleHealthSyncSerializer, AppleHealthTokenSerializer
from .models import AppleHealthConnection, FitnessAccount


class AppleHealthSyncView(APIView):
    authentication_classes = [AppleHealthAuthentication]
    parser_classes = [JSONParser]

    def post(self, request):
        serializer = AppleHealthSyncSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        digest = hashlib.sha256(json.dumps(request.data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        result = ingest(request.user, request.auth, serializer.validated_data, digest, request.headers.get("X-Device-Id"))
        return Response(result)


class AppleHealthTokenView(APIView):
    """Uses normal account JWT authentication; upload tokens cannot manage credentials or read fitness data."""
    def get(self, request):
        connection = AppleHealthConnection.objects.select_related("source").filter(user=request.user).first()
        account = FitnessAccount.objects.filter(user=request.user).first()
        return Response({
            "enabled": bool(connection and connection.token_hash),
            "active": bool(connection and account and account.active_source_id == connection.source_id),
            "device_id": connection.device_id if connection else None,
            "installation_id": str(connection.source.installation_id) if connection else None,
            "last_successful_sync_at": connection.source.last_successful_sync_at if connection else None,
            "backend_url": request.build_absolute_uri("/").rstrip("/"),
        }, headers={"Cache-Control": "no-store"})

    def post(self, request):
        serializer = AppleHealthTokenSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        connection, token = issue_token(request.user, **serializer.validated_data)
        return Response({"token": token, "backend_url": request.build_absolute_uri("/").rstrip("/"),
                         "installation_id": str(connection.source.installation_id), "device_id": connection.device_id},
                        status=201, headers={"Cache-Control": "no-store"})

    def delete(self, request):
        revoke_token(request.user)
        return Response(status=204)
