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
