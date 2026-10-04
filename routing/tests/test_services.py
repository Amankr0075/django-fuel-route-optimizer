"""
Tests for route utility functions (haversine, cross-track, along-track, etc.)
and geocoding/routing services with mocked HTTP calls.
"""
import math
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.test import TestCase

from routing.services.geocoding_service import (
    GeocodedLocation,
    GeocodingError,
    LocationNotInUSAError,
    geocode_location,
)
from routing.services.route_utils import (
    RoutePoint,
    build_route_points,
    cross_track_distance_miles,
    decode_polyline,
    haversine_miles,
    meters_to_miles,
    seconds_to_minutes,
    station_route_progress,
)
from routing.services.routing_service import (
    NoRouteFoundError,
    RouteResult,
    RoutingError,
    get_driving_route,
)


class HaversineTest(TestCase):
    """Tests for the haversine distance formula."""

    def test_same_point_zero_distance(self):
        dist = haversine_miles(40.7128, -74.0060, 40.7128, -74.0060)
        self.assertAlmostEqual(dist, 0.0, places=5)

    def test_new_york_to_chicago(self):
        """NY to Chicago is approximately 713 straight-line miles."""
        dist = haversine_miles(40.7128, -74.0060, 41.8781, -87.6298)
        self.assertGreater(dist, 700)
        self.assertLess(dist, 730)

    def test_symmetry(self):
        """Distance A→B should equal B→A."""
        d1 = haversine_miles(40.0, -74.0, 41.0, -87.0)
        d2 = haversine_miles(41.0, -87.0, 40.0, -74.0)
        self.assertAlmostEqual(d1, d2, places=5)

    def test_known_short_distance(self):
        """1 degree of latitude ≈ 69 miles."""
        dist = haversine_miles(40.0, -74.0, 41.0, -74.0)
        self.assertAlmostEqual(dist, 69.0, delta=1.0)


class CrossTrackDistanceTest(TestCase):
    """Tests for cross-track distance calculation.

    Note: The great circle between two points at the SAME latitude curves
    northward (it is NOT the rhumb line / constant-latitude line).  A point
    at the same latitude as the two endpoints will therefore have a non-zero
    cross-track distance.  Tests use an actual interpolated midpoint to avoid
    this confusion.
    """

    def test_interpolated_midpoint_has_near_zero_cross_track(self):
        """
        A point computed as the linear midpoint of a short segment should
        have very small (< 5 miles) cross-track distance.
        """
        # Short segment: Pittsburgh → Cleveland (similar latitudes)
        start_lat, start_lon = 40.44, -79.99  # Pittsburgh
        end_lat, end_lon = 41.50, -81.69       # Cleveland
        # Midpoint computed by linear interpolation
        mid_lat = (start_lat + end_lat) / 2
        mid_lon = (start_lon + end_lon) / 2
        perp = cross_track_distance_miles(
            mid_lat, mid_lon,
            start_lat, start_lon,
            end_lat, end_lon,
        )
        # The linear midpoint is not exactly on the great circle, but very close
        self.assertLess(perp, 5.0)

    def test_point_far_from_route_has_large_cross_track(self):
        """A point 300+ miles south of the route should have large cross-track distance."""
        perp = cross_track_distance_miles(
            30.0, -80.5,  # Miami-ish (far south)
            40.0, -74.0,  # Route: New York
            41.8, -87.6,  # Route: Chicago
        )
        self.assertGreater(perp, 100)

    def test_non_negative_always(self):
        """Cross-track distance must always be non-negative."""
        perp = cross_track_distance_miles(
            35.0, -90.0,
            40.0, -74.0,
            41.8, -87.6,
        )
        self.assertGreaterEqual(perp, 0.0)


class BuildRoutePointsTest(TestCase):
    """Tests for build_route_points function."""

    def test_empty_coordinates(self):
        points = build_route_points([])
        self.assertEqual(points, [])

    def test_single_point(self):
        points = build_route_points([[-74.0, 40.0]])
        self.assertEqual(len(points), 1)
        self.assertAlmostEqual(points[0].cumulative_miles, 0.0, places=5)

    def test_geojson_order(self):
        """GeoJSON coords are [lon, lat] — verify the parser handles them correctly."""
        coords = [[-74.0060, 40.7128], [-87.6298, 41.8781]]
        points = build_route_points(coords)
        # First point should be at NY coordinates
        self.assertAlmostEqual(points[0].lat, 40.7128, places=4)
        self.assertAlmostEqual(points[0].lon, -74.0060, places=4)

    def test_cumulative_distance_increases(self):
        coords = [
            [-74.0, 40.0],
            [-80.0, 40.0],
            [-87.0, 40.0],
        ]
        points = build_route_points(coords)
        self.assertAlmostEqual(points[0].cumulative_miles, 0.0, places=3)
        self.assertGreater(points[1].cumulative_miles, 0)
        self.assertGreater(points[2].cumulative_miles, points[1].cumulative_miles)

    def test_total_distance_reasonable(self):
        """NY to Chicago route should be ~700 miles straight-line."""
        coords = [[-74.0060, 40.7128], [-87.6298, 41.8781]]
        points = build_route_points(coords)
        self.assertGreater(points[-1].cumulative_miles, 600)
        self.assertLess(points[-1].cumulative_miles, 800)


class UnitConversionTest(TestCase):
    """Tests for unit conversion helpers."""

    def test_meters_to_miles(self):
        self.assertAlmostEqual(meters_to_miles(1609.344), 1.0, places=5)
        self.assertAlmostEqual(meters_to_miles(0), 0.0, places=5)

    def test_seconds_to_minutes(self):
        self.assertAlmostEqual(seconds_to_minutes(60), 1.0, places=5)
        self.assertAlmostEqual(seconds_to_minutes(3600), 60.0, places=5)


class GeocodingServiceTest(TestCase):
    """Tests for geocoding service with mocked HTTP."""

    @patch("routing.services.geocoding_service.cache")
    @patch("routing.services.geocoding_service.httpx.Client")
    def test_geocode_returns_location_for_valid_usa_city(self, mock_client_cls, mock_cache):
        mock_cache.get.return_value = None
        mock_response = MagicMock()
        mock_response.json.return_value = [
            {
                "lat": "40.7128",
                "lon": "-74.0060",
                "display_name": "New York City, New York, USA",
                "address": {"country_code": "us"},
            }
        ]
        mock_response.raise_for_status.return_value = None
        mock_client_cls.return_value.__enter__.return_value.get.return_value = mock_response

        result = geocode_location("New York, NY")

        self.assertEqual(result.country_code, "US")
        self.assertAlmostEqual(result.latitude, 40.7128, places=4)
        self.assertAlmostEqual(result.longitude, -74.0060, places=4)

    @patch("routing.services.geocoding_service.cache")
    @patch("routing.services.geocoding_service.httpx.Client")
    def test_geocode_raises_for_empty_results(self, mock_client_cls, mock_cache):
        mock_cache.get.return_value = None
        mock_response = MagicMock()
        mock_response.json.return_value = []
        mock_response.raise_for_status.return_value = None
        mock_client_cls.return_value.__enter__.return_value.get.return_value = mock_response

        with self.assertRaises(GeocodingError):
            geocode_location("Nowhere Land XYZ")

    @patch("routing.services.geocoding_service.cache")
    @patch("routing.services.geocoding_service.httpx.Client")
    def test_geocode_raises_for_non_usa_location(self, mock_client_cls, mock_cache):
        mock_cache.get.return_value = None
        mock_response = MagicMock()
        mock_response.json.return_value = [
            {
                "lat": "43.7",
                "lon": "-79.4",
                "display_name": "Toronto, Canada",
                "address": {"country_code": "ca"},
            }
        ]
        mock_response.raise_for_status.return_value = None
        mock_client_cls.return_value.__enter__.return_value.get.return_value = mock_response

        with self.assertRaises(LocationNotInUSAError):
            geocode_location("Toronto, Canada")

    @patch("routing.services.geocoding_service.cache")
    def test_geocode_returns_cached_result(self, mock_cache):
        mock_cache.get.return_value = {
            "input_text": "New York, NY",
            "latitude": 40.7128,
            "longitude": -74.0060,
            "display_name": "New York, USA",
            "country_code": "US",
        }
        result = geocode_location("New York, NY")
        self.assertEqual(result.latitude, 40.7128)


class RoutingServiceTest(TestCase):
    """Tests for OSRM routing service with mocked HTTP."""

    @patch("routing.services.routing_service.cache")
    @patch("routing.services.routing_service.httpx.Client")
    def test_get_route_returns_result(self, mock_client_cls, mock_cache):
        mock_cache.get.return_value = None
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "code": "Ok",
            "routes": [
                {
                    "distance": 1_271_500.0,  # meters
                    "duration": 44_880.0,      # seconds
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[-74.0060, 40.7128], [-87.6298, 41.8781]],
                    },
                }
            ],
        }
        mock_response.raise_for_status.return_value = None
        mock_client_cls.return_value.__enter__.return_value.get.return_value = mock_response

        result = get_driving_route(40.7128, -74.0060, 41.8781, -87.6298)

        self.assertAlmostEqual(result.distance_miles, 790.0, delta=5.0)
        self.assertAlmostEqual(result.duration_minutes, 748.0, delta=5.0)
        self.assertEqual(result.geometry["type"], "LineString")

    @patch("routing.services.routing_service.cache")
    @patch("routing.services.routing_service.httpx.Client")
    def test_no_route_raises_error(self, mock_client_cls, mock_cache):
        mock_cache.get.return_value = None
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "code": "NoRoute",
            "message": "No route found",
        }
        mock_response.raise_for_status.return_value = None
        mock_client_cls.return_value.__enter__.return_value.get.return_value = mock_response

        with self.assertRaises(NoRouteFoundError):
            get_driving_route(0.0, 0.0, 0.0, 0.0)

    @patch("routing.services.routing_service.cache")
    def test_returns_cached_result(self, mock_cache):
        mock_cache.get.return_value = {
            "distance_miles": 790.4,
            "duration_minutes": 748.0,
            "geometry": {"type": "LineString", "coordinates": []},
        }
        result = get_driving_route(40.7128, -74.0060, 41.8781, -87.6298)
        self.assertAlmostEqual(result.distance_miles, 790.4, places=1)
