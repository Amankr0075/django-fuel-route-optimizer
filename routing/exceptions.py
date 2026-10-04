"""
Custom exception handler for the Fuel Route API.

Provides a consistent error response format:

{
    "error": {
        "code": "ERROR_CODE",
        "message": "Human-readable description."
    }
}
"""
import logging

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler

logger = logging.getLogger(__name__)


def custom_exception_handler(exc: Exception, context: dict) -> Response | None:
    """
    Custom DRF exception handler.

    Wraps DRF's default handler output in the standard error envelope.
    Also handles non-DRF exceptions gracefully.
    """
    # Let DRF handle its own exceptions first
    response = exception_handler(exc, context)

    if response is not None:
        # Wrap in standard error format
        original_data = response.data

        if isinstance(original_data, dict) and "error" in original_data:
            # Already formatted (e.g., from our views)
            return response

        # Format DRF validation errors
        if isinstance(original_data, dict):
            messages = []
            for field, errors in original_data.items():
                if isinstance(errors, list):
                    for error in errors:
                        if field == "non_field_errors":
                            messages.append(str(error))
                        else:
                            messages.append(f"{field}: {error}")
                else:
                    messages.append(f"{field}: {errors}")
            message = " | ".join(messages) if messages else str(original_data)
        elif isinstance(original_data, list):
            message = " | ".join(str(e) for e in original_data)
        else:
            message = str(original_data)

        response.data = {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": message,
            }
        }
        return response

    return None


def error_response(
    code: str,
    message: str,
    http_status: int = status.HTTP_400_BAD_REQUEST,
) -> Response:
    """Helper to create a standardised error response."""
    return Response(
        {"error": {"code": code, "message": message}},
        status=http_status,
    )
