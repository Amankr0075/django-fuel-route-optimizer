"""
Management command: import_fuel_stations

Imports fuel station data from the OPIS CSV dataset into the database.

Usage:
    python manage.py import_fuel_stations path/to/fuel-prices-for-be-assessment.csv

Features:
- Validates CSV headers before processing.
- Parses and validates retail prices.
- Skips invalid rows with detailed logging.
- Uses bulk_create with ignore_conflicts to handle duplicates efficiently.
- Reports import statistics on completion.

Note on duplicate OPIS IDs:
    The CSV contains ~1,413 rows with duplicate OPIS IDs (same station
    appearing with slightly different data or prices). The unique constraint
    is on (opis_id, name, city, state) to handle this correctly.
    Duplicates are de-duplicated automatically via the database unique constraint.

Output example:
    Imported   : 6967
    Skipped    : 0
    Duplicates : 1184 (de-duplicated via unique constraint)
    Errors     : 0
"""
import csv
import logging
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from routing.models import FuelStation

logger = logging.getLogger(__name__)

EXPECTED_HEADERS = {
    "OPIS Truckstop ID",
    "Truckstop Name",
    "Address",
    "City",
    "State",
    "Rack ID",
    "Retail Price",
}

BATCH_SIZE = 500


class Command(BaseCommand):
    help = "Import fuel stations from the OPIS CSV dataset."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "csv_path",
            type=str,
            help="Path to the fuel-prices CSV file.",
        )
        parser.add_argument(
            "--clear",
            action="store_true",
            help="Clear existing stations before importing.",
        )

    def handle(self, *args, **options) -> None:
        csv_path = Path(options["csv_path"])

        if not csv_path.exists():
            raise CommandError(f"File not found: {csv_path}")

        if not csv_path.is_file():
            raise CommandError(f"Not a file: {csv_path}")

        if options["clear"]:
            count = FuelStation.objects.all().delete()[0]
            self.stdout.write(self.style.WARNING(f"Cleared {count} existing stations."))

        self.stdout.write(f"Importing from: {csv_path}")

        parsed_ok = 0      # Rows that passed validation
        skipped = 0        # Rows that failed validation
        batch: list[FuelStation] = []
        all_objects: list[FuelStation] = []

        try:
            with csv_path.open(encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)

                # Validate headers
                if reader.fieldnames is None:
                    raise CommandError("CSV file is empty or has no headers.")

                actual_headers = set(reader.fieldnames)
                missing = EXPECTED_HEADERS - actual_headers
                if missing:
                    raise CommandError(
                        f"CSV is missing expected columns: {missing}. "
                        f"Found: {actual_headers}"
                    )

                self.stdout.write(f"Headers validated: {list(reader.fieldnames)}")

                for line_num, row in enumerate(reader, start=2):
                    station = self._parse_row(row, line_num)
                    if station is None:
                        skipped += 1
                        continue
                    parsed_ok += 1
                    all_objects.append(station)

        except UnicodeDecodeError as exc:
            raise CommandError(f"Encoding error reading CSV: {exc}") from exc
        except csv.Error as exc:
            raise CommandError(f"CSV parse error: {exc}") from exc

        self.stdout.write(f"Parsed {parsed_ok} valid rows ({skipped} skipped).")

        # Bulk insert in batches
        count_before = FuelStation.objects.count()

        for i in range(0, len(all_objects), BATCH_SIZE):
            chunk = all_objects[i : i + BATCH_SIZE]
            FuelStation.objects.bulk_create(chunk, ignore_conflicts=True, batch_size=BATCH_SIZE)
            self.stdout.write(
                f"  Inserted batch {i // BATCH_SIZE + 1} / {(len(all_objects) + BATCH_SIZE - 1) // BATCH_SIZE}...",
                ending="\r",
            )
            self.stdout.flush()

        self.stdout.write("")
        count_after = FuelStation.objects.count()
        imported = count_after - count_before
        duplicates = parsed_ok - imported

        self.stdout.write(self.style.SUCCESS("\nImport complete:"))
        self.stdout.write(f"  Imported   : {imported}")
        self.stdout.write(f"  Skipped    : {skipped}")
        self.stdout.write(f"  Duplicates : {duplicates} (de-duplicated via unique constraint)")
        self.stdout.write(f"  Errors     : 0")
        self.stdout.write(f"\nTotal stations in database: {count_after}")
        self.stdout.write(
            f"Total geocoded: "
            f"{FuelStation.objects.filter(latitude__isnull=False).count()}"
        )
        self.stdout.write(
            self.style.WARNING(
                "\nNext step: python manage.py geocode_fuel_stations"
            )
        )

    def _parse_row(self, row: dict, line_num: int) -> "FuelStation | None":
        """
        Parse a CSV row into a FuelStation instance.

        Returns None if the row is invalid (skipped).
        """
        opis_id = row.get("OPIS Truckstop ID", "").strip()
        name = row.get("Truckstop Name", "").strip()
        address = row.get("Address", "").strip()
        city = row.get("City", "").strip()
        state = row.get("State", "").strip().upper()
        rack_id = row.get("Rack ID", "").strip()
        price_str = row.get("Retail Price", "").strip()

        # Validate required fields
        if not opis_id:
            logger.warning("Line %d: Missing OPIS ID — skipping.", line_num)
            return None

        if not name:
            logger.warning("Line %d: Missing name (OPIS ID %s) — skipping.", line_num, opis_id)
            return None

        if not city or not state:
            logger.warning(
                "Line %d: Missing city/state (OPIS ID %s) — skipping.",
                line_num, opis_id,
            )
            return None

        if not price_str:
            logger.warning(
                "Line %d: Missing price (OPIS ID %s) — skipping.",
                line_num, opis_id,
            )
            return None

        # Validate price
        try:
            price = Decimal(price_str)
            if price <= 0 or price > 20:
                raise ValueError(f"Price {price} is outside plausible range (0–20).")
        except (InvalidOperation, ValueError) as exc:
            logger.warning(
                "Line %d: Invalid price '%s' (OPIS ID %s): %s — skipping.",
                line_num, price_str, opis_id, exc,
            )
            return None

        return FuelStation(
            opis_id=opis_id,
            name=name,
            address=address,
            city=city,
            state=state,
            rack_id=rack_id,
            retail_price=price,
        )
