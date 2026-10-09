"""One consistent JSON error shape: {"error": {"code", "message", "details"}}."""
import logging

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler

CODES = {
    400: "validation_error", 401: "unauthenticated", 403: "forbidden",
    404: "not_found", 405: "method_not_allowed", 409: "conflict", 429: "rate_limited",
}

logger = logging.getLogger("django.request")


def api_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if response is None:  # unhandled -> generic 500, never leak internals
        view = context.get("view")
        logger.error(
            "Unhandled API exception in %s",
            type(view).__name__ if view is not None else "unknown view",
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        return Response(
            {"error": {"code": "server_error", "message": "Something went wrong.", "details": None}},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    data = response.data
    message = data.get("detail") if isinstance(data, dict) and "detail" in data else "Request failed."
    details = None if isinstance(data, dict) and "detail" in data else data
    response.data = {
        "error": {
            "code": CODES.get(response.status_code, "error"),
            "message": str(message),
            "details": details,
        }
    }
    return response
