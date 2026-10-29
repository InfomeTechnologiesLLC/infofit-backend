from rest_framework.renderers import BaseRenderer as _BaseRenderer, JSONRenderer as _JSONRenderer
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from ai_service.serializers import ChatSerializer

from . import services


class AssistantMessageView(APIView):
    """Like ai/chat/, but may answer with a draft the employee can confirm."""
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "ai"

    def post(self, request):
        serializer = ChatSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        return Response(services.handle_message(request.user, data["message"], data.get("history", [])))


class ProposalConfirmView(APIView):
    def post(self, request, pk):
        proposal, message = services.confirm(request.user, pk)
        return Response({"status": proposal.status, "message": message,
                         "undo_available": proposal.status == "confirmed"})


class ProposalCancelView(APIView):
    def post(self, request, pk):
        proposal = services.cancel(request.user, pk)
        return Response({"status": proposal.status, "message": "Okay, I did not log anything.", "undo_available": False})


class ProposalUndoView(APIView):
    def post(self, request, pk):
        proposal = services.undo(request.user, pk)
        return Response({"status": proposal.status, "message": "Undone. That entry has been removed.", "undo_available": False})


# ---- IVA with tools, saved history and streaming --------------------------------------------------

import json as _json
import logging as _logging

from django.http import StreamingHttpResponse
from rest_framework import serializers as _serializers
from rest_framework.exceptions import APIException as _APIException

from accounts.models import EmployeeProfile

from . import agent
from .models import ChatTurn
from .tools import DIETS, PREFERENCE_KEYS, preferences

_log = _logging.getLogger("infome.assistant")


class AgentMessageSerializer(_serializers.Serializer):
    message = _serializers.CharField(max_length=1000, trim_whitespace=True)


class AgentChatView(APIView):
    """IVA with tools. History is kept on the server, so the app sends only the new message."""
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "ai"

    def post(self, request):
        serializer = AgentMessageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(agent.run(request.user, serializer.validated_data["message"]))


def _sse(event, data):
    return f"event: {event}\ndata: {_json.dumps(data, default=str)}\n\n"


class EventStreamRenderer(_BaseRenderer):
    """Lets DRF accept `Accept: text/event-stream`. Without it DRF answers 406 before the view runs.

    The stream itself is a StreamingHttpResponse and never goes through a renderer; this only renders error
    bodies (validation, throttling, auth) as JSON text so the app can still read them.
    """
    media_type = "text/event-stream"
    format = "event-stream"
    charset = "utf-8"

    def render(self, data, accepted_media_type=None, renderer_context=None):
        return _json.dumps(data, default=str).encode("utf-8")


class AgentStreamView(APIView):
    """Same as AgentChatView, streamed as server-sent events: `delta` events with text, then `done` (or `error`)."""
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "ai"
    renderer_classes = [_JSONRenderer, EventStreamRenderer]

    def post(self, request):
        serializer = AgentMessageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user, message = request.user, serializer.validated_data["message"]

        def events():
            try:
                for event, data in agent.run_events(user, message, streaming=True):
                    yield _sse(event, data)
            except _APIException as exc:
                yield _sse("error", {"message": str(exc.detail)})
            except Exception as exc:
                _log.exception("IVA stream failed")
                from ai_service.provider import record_failure
                record_failure("Server error in IVA: {}: {}".format(type(exc).__name__, exc)[:300])
                yield _sse("error", {"message": "IVA is unavailable right now. Please try again later."})

        response = StreamingHttpResponse(events(), content_type="text/event-stream")
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"  # stop nginx from holding the stream back
        return response


class ChatHistoryView(APIView):
    def get(self, request):
        before = request.query_params.get("before")
        before = int(before) if before and before.isdigit() else None
        return Response(agent.history_payload(request.user, before=before))

    def delete(self, request):
        ChatTurn.objects.filter(user=request.user).delete()
        return Response({"cleared": True})


class PreferencesSerializer(_serializers.Serializer):
    diet = _serializers.ChoiceField(choices=DIETS, required=False, allow_blank=True)
    cuisine = _serializers.CharField(max_length=PREFERENCE_KEYS["cuisine"], required=False, allow_blank=True)
    dislikes = _serializers.CharField(max_length=PREFERENCE_KEYS["dislikes"], required=False, allow_blank=True)
    activity = _serializers.CharField(max_length=PREFERENCE_KEYS["activity"], required=False, allow_blank=True)
    notes = _serializers.CharField(max_length=PREFERENCE_KEYS["notes"], required=False, allow_blank=True)


class PreferencesView(APIView):
    """What IVA remembers about food and activity. A blank value forgets that preference."""
    def get(self, request):
        return Response(preferences(request.user))

    def patch(self, request):
        serializer = PreferencesSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        profile, _ = EmployeeProfile.objects.get_or_create(user=request.user)
        prefs = dict(profile.wellness_preferences or {})
        iva = dict(prefs.get("iva") or {})
        for key, value in serializer.validated_data.items():
            value = " ".join(str(value).split())
            if value:
                iva[key] = value
            else:
                iva.pop(key, None)
        prefs["iva"] = iva
        profile.wellness_preferences = prefs
        profile.save(update_fields=["wellness_preferences", "updated_at"])
        return Response(preferences(request.user))
