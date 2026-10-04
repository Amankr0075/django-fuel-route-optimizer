"""
Tests for the POST /api/v1/route/ and GET /api/v1/health/ endpoints.

All external API calls (Nominatim, OSRM) are mocked.
"""
import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from routing.models import FuelStation
from routing.services.geocoding_service import GeocodedLocation, GeocodingError, LocationNotInUSAError
from routing.services.routing_service import NoRouteFoundError, RouteResult


def make_geocoded(lat=40.7128, lon=-74.0060, input_text="New York, NY") -> GeocodedLocation:
    return GeocodedLocation(
        input_text=input_text,
        latitude=lat,
        longitude=lon,
        display_name=f"{input_text}, USA",
        country_code="US",
    )


def make_route_result(distance_miles=790.4, duration_minutes=748.0) -> RouteResult:
    return RouteResult(
        distance_miles=distance_miles,
        duration_minutes=duration_minutes,
        geometry={
            "type": "LineString",
            "coordinates": [
                [-74.0060, 40.7128],
                [-80.0, 41.5],
                [-87.6298, 41.8781],
            ],
        },
    )


def make_station(
    opis_id="1",
    name="Test Station",
    city="Columbus",
    state="OH",
    price=Decimal("3.05"),
    lat=39.96,
    lon=-82.99,
) -> FuelStation:
    station = FuelStation(
        opis_id=opis_id,
        name=name,
        address="100 Main St",
        city=city,
        state=state,
        rack_id="100",
        retail_price=price,
        latitude=lat,
        longitude=lon,
    )
    station.save()
    return station


class HealthCheckViewTest(APITestCase):
    """Tests for GET /api/v1/health/"""

    def test_health_check_returns_200(self):
        url = reverse("routing:health")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data["status"], "ok")
        self.assertIn("database", data)
        self.assertIn("vehicle", data)

    def test_health_check_vehicle_constants(self):
        url = reverse("routing:health")
        response = self.client.get(url)
        data = response.json()
        self.assertEqual(data["vehicle"]["max_range_miles"], 500)
        self.assertEqual(data["vehicle"]["fuel_efficiency_mpg"], 10)


class RouteViewValidationTest(APITestCase):
    """Tests for request validation on POST /api/v1/route/"""

    def get_url(self):
        return reverse("routing:route")

    def test_missing_start_returns_400(self):
        response = self.client.post(self.get_url(), {"finish": "Chicago, IL"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], "INVALID_REQUEST")

    def test_missing_finish_returns_400(self):
        response = self.client.post(self.get_url(), {"start": "New York, NY"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        data = response.json()
        self.assertIn("error", data)

    def test_empty_body_returns_400(self):
        response = self.client.post(self.get_url(), {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_blank_start_returns_400(self):
        response = self.client.post(
            self.get_url(), {"start": "   ", "finish": "Chicago, IL"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_same_start_and_finish_returns_400(self):
        response = self.client.post(
            self.get_url(),
            {"start": "New York, NY", "finish": "New York, NY"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        data = response.json()
        self.assertIn("error", data)

    @patch("routing.views.geocode_location")
    def test_invalid_location_returns_404(self, mock_geocode):
        mock_geocode.side_effect = GeocodingError("No results found.")
        response = self.client.post(
            self.get_url(),
            {"start": "Nowhere Land", "finish": "Chicago, IL"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        data = response.json()
        self.assertEqual(data["error"]["code"], "INVALID_LOCATION")

    @patch("routing.views.geocode_location")
    def test_non_usa_location_returns_422(self, mock_geocode):
        mock_geocode.side_effect = LocationNotInUSAError("Location is in Canada.")
        response = self.client.post(
            self.get_url(),
            {"start": "Toronto, Canada", "finish": "Chicago, IL"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        data = response.json()
        self.assertEqual(data["error"]["code"], "LOCATION_NOT_IN_USA")

    @patch("routing.views.get_driving_route")
    @patch("routing.views.geocode_location")
    def test_no_route_found_returns_404(self, mock_geocode, mock_route):
        mock_geocode.return_value = make_geocoded()
        mock_route.side_effect = NoRouteFoundError("No route.")
        response = self.client.post(
            self.get_url(),
            {"start": "New York, NY", "finish": "Chicago, IL"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        data = response.json()
        self.assertEqual(data["error"]["code"], "NO_ROUTE_FOUND")


class RouteViewSuccessTest(APITestCase):
    """Tests for successful route calculation."""

    def get_url(self):
        return reverse("routing:route")

    def _setup_stations(self):
        """Create stations along the NY→Chicago route."""
        # Station near the midpoint of our mock route
        make_station(
            opis_id="101",
            name="Midpoint Station",
            city="Columbus",
            state="OH",
            price=Decimal("3.05"),
            lat=40.0,
            lon=-82.0,
        )

    @patch("routing.views.optimize_fuel_stops")
    @patch("routing.views.get_driving_route")
    @patch("routing.views.geocode_location")
    def test_valid_request_returns_200(self, mock_geocode, mock_route, mock_optimize):
        from routing.services.fuel_optimizer import FuelPlan
        mock_geocode.side_effect = [
            make_geocoded(40.7128, -74.0060, "New York, NY"),
            make_geocoded(41.8781, -87.6298, "Chicago, IL"),
        ]
        mock_route.return_value = make_route_result(790.4)
        mock_optimize.return_value = FuelPlan(
            fuel_stops=[],
            total_gallons=79.04,
            total_cost=Decimal("240.50"),
        )

        response = self.client.post(
            self.get_url(),
            {"start": "New York, NY", "finish": "Chicago, IL"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()

        # Check structure
        self.assertIn("start", data)
        self.assertIn("finish", data)
        self.assertIn("route", data)
        self.assertIn("vehicle", data)
        self.assertIn("fuel_stops", data)
        self.assertIn("fuel", data)

    @patch("routing.views.optimize_fuel_stops")
    @patch("routing.views.get_driving_route")
    @patch("routing.views.geocode_location")
    def test_response_contains_route_geometry(self, mock_geocode, mock_route, mock_optimize):
        from routing.services.fuel_optimizer import FuelPlan
        mock_geocode.side_effect = [
            make_geocoded(40.7128, -74.0060, "New York, NY"),
            make_geocoded(41.8781, -87.6298, "Chicago, IL"),
        ]
        mock_route.return_value = make_route_result()
        mock_optimize.return_value = FuelPlan(
            fuel_stops=[], total_gallons=79.04, total_cost=Decimal("240.50")
        )

        response = self.client.post(
            self.get_url(),
            {"start": "New York, NY", "finish": "Chicago, IL"},
            format="json",
        )
        data = response.json()
        self.assertEqual(data["route"]["geometry"]["type"], "LineString")
        self.assertIn("coordinates", data["route"]["geometry"])

    @patch("routing.views.optimize_fuel_stops")
    @patch("routing.views.get_driving_route")
    @patch("routing.views.geocode_location")
    def test_vehicle_constants_in_response(self, mock_geocode, mock_route, mock_optimize):
        from routing.services.fuel_optimizer import FuelPlan
        mock_geocode.side_effect = [
            make_geocoded(40.7128, -74.0060, "New York, NY"),
            make_geocoded(41.8781, -87.6298, "Chicago, IL"),
        ]
        mock_route.return_value = make_route_result()
        mock_optimize.return_value = FuelPlan(
            fuel_stops=[], total_gallons=79.04, total_cost=Decimal("0")
        )
        response = self.client.post(
            self.get_url(),
            {"start": "New York, NY", "finish": "Chicago, IL"},
            format="json",
        )
        data = response.json()
        self.assertEqual(data["vehicle"]["max_range_miles"], 500)
        self.assertEqual(data["vehicle"]["fuel_efficiency_mpg"], 10)

    @patch("routing.views.optimize_fuel_stops")
    @patch("routing.views.get_driving_route")
    @patch("routing.views.geocode_location")
    def test_no_feasible_plan_returns_422(self, mock_geocode, mock_route, mock_optimize):
        from routing.services.fuel_optimizer import NoFeasiblePlanError
        mock_geocode.side_effect = [
            make_geocoded(40.7128, -74.0060, "New York, NY"),
            make_geocoded(41.8781, -87.6298, "Chicago, IL"),
        ]
        mock_route.return_value = make_route_result()
        mock_optimize.side_effect = NoFeasiblePlanError("No feasible plan.")
        response = self.client.post(
            self.get_url(),
            {"start": "New York, NY", "finish": "Chicago, IL"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        data = response.json()
        self.assertEqual(data["error"]["code"], "NO_FEASIBLE_FUEL_PLAN")
