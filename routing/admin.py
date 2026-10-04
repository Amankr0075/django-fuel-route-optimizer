"""
Django admin registration for FuelStation.
"""
from django.contrib import admin
from .models import FuelStation


@admin.register(FuelStation)
class FuelStationAdmin(admin.ModelAdmin):
    """Admin configuration for FuelStation model."""

    list_display = (
        "opis_id",
        "name",
        "city",
        "state",
        "retail_price",
        "has_coordinates",
        "updated_at",
    )
    list_filter = ("state",)
    search_fields = ("name", "city", "opis_id", "address")
    ordering = ("state", "city", "retail_price")
    readonly_fields = ("created_at", "updated_at")

    list_per_page = 50

    @admin.display(boolean=True, description="Has Coordinates")
    def has_coordinates(self, obj: FuelStation) -> bool:
        return obj.has_coordinates
