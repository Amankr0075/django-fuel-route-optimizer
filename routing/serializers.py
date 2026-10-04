"""
Request/Response serializers for the Fuel Route API.
"""
from rest_framework import serializers


class RouteRequestSerializer(serializers.Serializer):
    """Validates the POST /api/v1/route/ request body."""

    start = serializers.CharField(
        max_length=500,
        help_text="Start location within the USA (e.g., 'New York, NY').",
    )
    finish = serializers.CharField(
        max_length=500,
        help_text="Finish/destination location within the USA (e.g., 'Chicago, IL').",
    )

    def validate_start(self, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise serializers.ValidationError("Start location must not be blank.")
        return stripped

    def validate_finish(self, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise serializers.ValidationError("Finish location must not be blank.")
        return stripped

    def validate(self, data: dict) -> dict:
        start = data.get("start", "").strip().lower()
        finish = data.get("finish", "").strip().lower()
        if start and finish and start == finish:
            raise serializers.ValidationError(
                {
                    "non_field_errors": [
                        "Start and finish locations must be different."
                    ]
                }
            )
        return data
