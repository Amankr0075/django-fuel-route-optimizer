"""
FuelStation model — represents a fuel station loaded from the OPIS dataset.
"""
from decimal import Decimal
from django.db import models


class FuelStation(models.Model):
    """
    Represents a fuel station from the OPIS truckstop dataset.

    Latitude/longitude are populated during the geocoding preprocessing step
    (python manage.py geocode_fuel_stations).  Stations without coordinates
    are excluded from route optimization.
    """

    opis_id = models.CharField(
        max_length=20,
        db_index=True,
        verbose_name="OPIS Truckstop ID",
        help_text="Original OPIS identifier from the CSV dataset.",
    )
    name = models.CharField(max_length=255, verbose_name="Truckstop Name")
    address = models.CharField(max_length=500)
    city = models.CharField(max_length=100, db_index=True)
    state = models.CharField(max_length=10, db_index=True)
    rack_id = models.CharField(max_length=20, blank=True, default="")
    retail_price = models.DecimalField(
        max_digits=10,
        decimal_places=5,
        verbose_name="Retail Price (USD/gallon)",
    )

    # Geocoded coordinates — NULL until geocoding step completes.
    latitude = models.FloatField(null=True, blank=True, db_index=True)
    longitude = models.FloatField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Fuel Station"
        verbose_name_plural = "Fuel Stations"
        # Enforce uniqueness on (opis_id, name, city, state) because the CSV
        # contains duplicate OPIS IDs (same station, possibly different prices
        # or locations in the raw data).
        unique_together = [("opis_id", "name", "city", "state")]
        indexes = [
            models.Index(fields=["state", "retail_price"]),
            models.Index(fields=["latitude", "longitude"]),
        ]
        ordering = ["state", "city", "name"]

    def __str__(self) -> str:
        return f"{self.name} — {self.city}, {self.state} (${self.retail_price}/gal)"

    @property
    def has_coordinates(self) -> bool:
        """True if this station has been successfully geocoded."""
        return self.latitude is not None and self.longitude is not None

    @property
    def price_display(self) -> str:
        """Human-readable price rounded to 2 decimal places."""
        return f"${Decimal(str(self.retail_price)).quantize(Decimal('0.01'))}"
