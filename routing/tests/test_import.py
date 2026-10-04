"""
Tests for the import_fuel_stations management command.

Covers:
- Valid CSV import
- Malformed rows
- Duplicate station handling
- Invalid price handling
- Missing required fields
"""
import csv
import tempfile
from decimal import Decimal
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from routing.models import FuelStation


def create_csv(rows: list[dict], path: Path) -> None:
    """Write a CSV file with the expected headers and given rows."""
    headers = [
        "OPIS Truckstop ID",
        "Truckstop Name",
        "Address",
        "City",
        "State",
        "Rack ID",
        "Retail Price",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


VALID_ROW_1 = {
    "OPIS Truckstop ID": "7",
    "Truckstop Name": "WOODSHED OF BIG CABIN",
    "Address": "I-44, EXIT 283",
    "City": "Big Cabin",
    "State": "OK",
    "Rack ID": "307",
    "Retail Price": "3.00733333",
}

VALID_ROW_2 = {
    "OPIS Truckstop ID": "9",
    "Truckstop Name": "KWIK TRIP #796",
    "Address": "I-94, EXIT 143",
    "City": "Tomah",
    "State": "WI",
    "Rack ID": "420",
    "Retail Price": "3.28733333",
}


class ImportFuelStationsTest(TestCase):
    """Tests for the import_fuel_stations management command."""

    def test_valid_csv_imports_stations(self):
        """A valid CSV should import all stations."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8"
        ) as tmp:
            tmp_path = Path(tmp.name)

        create_csv([VALID_ROW_1, VALID_ROW_2], tmp_path)

        out = StringIO()
        call_command("import_fuel_stations", str(tmp_path), stdout=out)

        self.assertEqual(FuelStation.objects.count(), 2)
        tmp_path.unlink()

    def test_station_data_is_correct(self):
        """Imported station should have correct field values."""
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
            tmp_path = Path(tmp.name)

        create_csv([VALID_ROW_1], tmp_path)
        call_command("import_fuel_stations", str(tmp_path), stdout=StringIO())

        station = FuelStation.objects.get(opis_id="7", name="WOODSHED OF BIG CABIN")
        self.assertEqual(station.city, "Big Cabin")
        self.assertEqual(station.state, "OK")
        self.assertAlmostEqual(float(station.retail_price), 3.00733333, places=5)
        tmp_path.unlink()

    def test_duplicate_station_not_imported_twice(self):
        """Same station imported twice should only appear once."""
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
            tmp_path = Path(tmp.name)

        create_csv([VALID_ROW_1, VALID_ROW_1], tmp_path)  # Duplicate rows
        call_command("import_fuel_stations", str(tmp_path), stdout=StringIO())

        self.assertEqual(FuelStation.objects.filter(opis_id="7").count(), 1)
        tmp_path.unlink()

    def test_invalid_price_row_is_skipped(self):
        """Row with invalid price should be skipped, valid rows still imported."""
        bad_row = {**VALID_ROW_1, "Retail Price": "not_a_number"}

        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
            tmp_path = Path(tmp.name)

        create_csv([bad_row, VALID_ROW_2], tmp_path)
        call_command("import_fuel_stations", str(tmp_path), stdout=StringIO())

        self.assertEqual(FuelStation.objects.count(), 1)
        self.assertEqual(FuelStation.objects.first().name, "KWIK TRIP #796")
        tmp_path.unlink()

    def test_negative_price_row_is_skipped(self):
        """Row with negative/zero price should be skipped."""
        bad_row = {**VALID_ROW_1, "Retail Price": "-1.5"}

        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
            tmp_path = Path(tmp.name)

        create_csv([bad_row], tmp_path)
        call_command("import_fuel_stations", str(tmp_path), stdout=StringIO())

        self.assertEqual(FuelStation.objects.count(), 0)
        tmp_path.unlink()

    def test_missing_city_row_is_skipped(self):
        """Row with missing city should be skipped."""
        bad_row = {**VALID_ROW_1, "City": ""}

        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
            tmp_path = Path(tmp.name)

        create_csv([bad_row, VALID_ROW_2], tmp_path)
        call_command("import_fuel_stations", str(tmp_path), stdout=StringIO())

        self.assertEqual(FuelStation.objects.count(), 1)
        tmp_path.unlink()

    def test_missing_opis_id_row_is_skipped(self):
        """Row with missing OPIS ID should be skipped."""
        bad_row = {**VALID_ROW_1, "OPIS Truckstop ID": ""}

        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
            tmp_path = Path(tmp.name)

        create_csv([bad_row, VALID_ROW_2], tmp_path)
        call_command("import_fuel_stations", str(tmp_path), stdout=StringIO())

        self.assertEqual(FuelStation.objects.count(), 1)
        tmp_path.unlink()

    def test_missing_file_raises_command_error(self):
        """Non-existent file should raise CommandError."""
        with self.assertRaises(CommandError):
            call_command(
                "import_fuel_stations",
                "/nonexistent/path/to/file.csv",
                stdout=StringIO(),
            )

    def test_wrong_headers_raises_command_error(self):
        """CSV with wrong headers should raise CommandError."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write("wrong,headers,here\n")
            tmp.write("1,2,3\n")
            tmp_path = Path(tmp.name)

        with self.assertRaises(CommandError):
            call_command("import_fuel_stations", str(tmp_path), stdout=StringIO())

        tmp_path.unlink()

    def test_clear_flag_removes_existing_stations(self):
        """--clear flag should delete existing stations before import."""
        # Pre-populate
        FuelStation.objects.create(
            opis_id="999",
            name="Old Station",
            address="Old",
            city="Old City",
            state="TX",
            retail_price=Decimal("4.00"),
        )
        self.assertEqual(FuelStation.objects.count(), 1)

        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
            tmp_path = Path(tmp.name)

        create_csv([VALID_ROW_1], tmp_path)
        call_command("import_fuel_stations", str(tmp_path), "--clear", stdout=StringIO())

        # Old station should be gone; only new one remains
        self.assertEqual(FuelStation.objects.count(), 1)
        self.assertEqual(FuelStation.objects.first().name, "WOODSHED OF BIG CABIN")
        tmp_path.unlink()
