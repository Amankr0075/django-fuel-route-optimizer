"""
Management command: geocode_fuel_stations

Geocodes fuel stations using a local US cities dataset (simplemaps.com),
with an optional Nominatim fallback for cities not found locally.

Primary strategy: Local US Cities CSV (data/uscities.csv)
-------------------------------------------------------
- No API calls required.
- No rate limits.
- Covers 30,000+ US cities.
- Runs in minutes, not hours.
- Data from simplemaps.com (free basic license).

Fallback: Nominatim (OpenStreetMap)
------------------------------------
- Used only for cities not found in the local dataset.
- Respects 1 req/sec rate limit.
- Requires NOMINATIM_USER_AGENT to be set with a real email address.

Geocoding strategy: city + state level
----------------------------------------
All stations in the same city share the same coordinates.
This is accurate enough for the 10-mile route proximity filter.
Full-address geocoding would be unreliable for highway exit addresses.

Usage:
    # Geocode using local dataset (recommended — fast, no API limits)
    python manage.py geocode_fuel_stations

    # Geocode specific state only
    python manage.py geocode_fuel_stations --state TX

    # Dry run (preview what would be geocoded)
    python manage.py geocode_fuel_stations --dry-run

    # Re-geocode stations that already have coordinates
    python manage.py geocode_fuel_stations --overwrite

    # Limit for testing
    python manage.py geocode_fuel_stations --limit 50
"""
import csv
import logging
import time
from collections import defaultdict
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from routing.models import FuelStation
from routing.services.geocoding_service import geocode_city_state

logger = logging.getLogger(__name__)

# Path to local US cities dataset (simplemaps.com free basic license)
DEFAULT_CITIES_CSV = Path(settings.BASE_DIR) / "data" / "uscities.csv"

# Nominatim rate limit
_NOMINATIM_SLEEP = 1.2


def load_city_coords(cities_csv: Path) -> dict[tuple[str, str], tuple[float, float]]:
    """
    Load the simplemaps US cities CSV into a (city_lower, state_id) → (lat, lon) dict.

    The CSV uses state abbreviations (state_id) which match our fuel station data.
    """
    coords: dict[tuple[str, str], tuple[float, float]] = {}
    if not cities_csv.exists():
        return coords

    with cities_csv.open(encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            city = row.get("city_ascii", row.get("city", "")).strip().lower()
            state = row.get("state_id", "").strip().upper()
            lat_str = row.get("lat", "").strip()
            lng_str = row.get("lng", "").strip()
            if city and state and lat_str and lng_str:
                try:
                    coords[(city, state)] = (float(lat_str), float(lng_str))
                except ValueError:
                    continue
    return coords


class Command(BaseCommand):
    help = "Geocode fuel stations using a local US cities dataset."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--state",
            type=str,
            default=None,
            help="Only geocode stations in this state (e.g., TX).",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Maximum number of unique city+state pairs to geocode.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be geocoded without updating the database.",
        )
        parser.add_argument(
            "--overwrite",
            action="store_true",
            help="Re-geocode stations that already have coordinates.",
        )
        parser.add_argument(
            "--cities-csv",
            type=str,
            default=str(DEFAULT_CITIES_CSV),
            help=f"Path to the US cities CSV file (default: {DEFAULT_CITIES_CSV})",
        )
        parser.add_argument(
            "--nominatim-fallback",
            action="store_true",
            help="Use Nominatim API for cities not found in the local dataset.",
        )

    def handle(self, *args, **options) -> None:
        state_filter = options.get("state")
        limit = options.get("limit")
        dry_run = options.get("dry_run", False)
        overwrite = options.get("overwrite", False)
        use_nominatim = options.get("nominatim_fallback", False)
        cities_csv_path = Path(options["cities_csv"])

        # Load local city coordinates
        self.stdout.write(f"Loading local city data from: {cities_csv_path}")
        city_coords = load_city_coords(cities_csv_path)

        if city_coords:
            self.stdout.write(
                self.style.SUCCESS(f"Loaded {len(city_coords):,} city coordinates from local dataset.")
            )
        else:
            self.stdout.write(
                self.style.WARNING(
                    f"Local cities CSV not found at {cities_csv_path}. "
                    "Will use Nominatim for all lookups."
                )
            )

        # Build query
        qs = FuelStation.objects.all()
        if state_filter:
            qs = qs.filter(state__iexact=state_filter)
            self.stdout.write(f"Filtering to state: {state_filter.upper()}")
        if not overwrite:
            qs = qs.filter(latitude__isnull=True)

        # Group by (city, state)
        city_state_to_ids: dict[tuple[str, str], list[int]] = defaultdict(list)
        for station in qs.values("id", "city", "state"):
            key = (station["city"], station["state"])
            city_state_to_ids[key].append(station["id"])

        unique_pairs = list(city_state_to_ids.items())
        if limit:
            unique_pairs = unique_pairs[:limit]

        total = len(unique_pairs)
        station_total = sum(len(v) for _, v in unique_pairs)
        self.stdout.write(
            f"Found {total} unique city+state pairs ({station_total} stations) to geocode."
        )

        if dry_run:
            for i, ((city, state), ids) in enumerate(unique_pairs[:20], 1):
                key = (city.lower(), state.upper())
                found = key in city_coords
                self.stdout.write(
                    f"  {i}. {city}, {state} ({len(ids)} stations) "
                    f"{'[in local dataset]' if found else '[needs Nominatim]'}"
                )
            if total > 20:
                self.stdout.write(f"  ... and {total - 20} more")
            self.stdout.write("[DRY RUN — no database changes]")
            return

        geocoded = 0
        local_hits = 0
        nominatim_hits = 0
        failed = 0

        for i, ((city, state), station_ids) in enumerate(unique_pairs, 1):
            self.stdout.write(
                f"[{i}/{total}] Geocoding: {city}, {state} ...",
                ending=" ",
            )
            self.stdout.flush()

            # Try local dataset first
            key = (city.lower(), state.upper())
            coords = city_coords.get(key)

            if coords:
                lat, lon = coords
                source = "local"
                local_hits += 1
            elif use_nominatim:
                # Fall back to Nominatim
                coords = geocode_city_state(city, state)
                if coords:
                    lat, lon = coords
                    source = "nominatim"
                    nominatim_hits += 1
                else:
                    self.stdout.write(self.style.WARNING("NOT FOUND"))
                    failed += 1
                    time.sleep(_NOMINATIM_SLEEP)
                    continue
                time.sleep(_NOMINATIM_SLEEP)
            else:
                self.stdout.write(self.style.WARNING("NOT IN LOCAL DATASET (use --nominatim-fallback)"))
                failed += 1
                continue

            # Update all stations in this city+state
            updated = FuelStation.objects.filter(id__in=station_ids).update(
                latitude=lat,
                longitude=lon,
            )
            geocoded += updated

            self.stdout.write(
                self.style.SUCCESS(
                    f"({lat:.4f}, {lon:.4f}) [{source}] — {updated} stations"
                )
            )

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("\nGeocoding complete:"))
        self.stdout.write(f"  Geocoded stations : {geocoded}")
        self.stdout.write(f"  From local dataset: {local_hits}")
        self.stdout.write(f"  From Nominatim    : {nominatim_hits}")
        self.stdout.write(f"  Not found         : {failed}")
        total_with_coords = FuelStation.objects.filter(latitude__isnull=False).count()
        self.stdout.write(f"  Total with coords : {total_with_coords}")
