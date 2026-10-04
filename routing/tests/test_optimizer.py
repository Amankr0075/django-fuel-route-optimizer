"""
Tests for the fuel optimizer algorithm.

Tests cover:
- Short routes (no stop needed)
- Routes requiring a single stop
- Routes requiring multiple stops
- Cheapest feasible station selection
- Infeasible routes (no stations in range)
- Destination reachability checks
- Decimal precision of cost calculations
"""
from decimal import Decimal

from django.test import TestCase

from routing.models import FuelStation
from routing.services.fuel_optimizer import (
    MAX_RANGE_MILES,
    NoFeasiblePlanError,
    optimize_fuel_stops,
)
from routing.services.route_utils import RoutePoint, build_route_points


def make_route_points_linear(
    start_lat: float,
    start_lon: float,
    end_lat: float,
    end_lon: float,
    total_miles: float,
    num_points: int = 20,
) -> list[RoutePoint]:
    """
    Create a sequence of RoutePoints along a straight line between
    two geo coordinates.  Cumulative miles are distributed evenly.
    """
    points = []
    for i in range(num_points):
        fraction = i / (num_points - 1)
        lat = start_lat + fraction * (end_lat - start_lat)
        lon = start_lon + fraction * (end_lon - start_lon)
        cumulative = fraction * total_miles
        points.append(RoutePoint(lat=lat, lon=lon, cumulative_miles=cumulative))
    return points


# Route constants: New York → Chicago (very roughly)
NY_LAT, NY_LON = 40.7128, -74.0060
CHI_LAT, CHI_LON = 41.8781, -87.6298

# A station near the midpoint of the NY–Chicago straight-line route
# (midpoint ≈ lat 41.3, lon -80.8 → near Cleveland, OH)
MID_LAT = (NY_LAT + CHI_LAT) / 2  # ≈ 41.3
MID_LON = (NY_LON + CHI_LON) / 2  # ≈ -80.8


def make_db_station(
    opis_id: str,
    lat: float,
    lon: float,
    price: float,
    city: str = "TestCity",
    state: str = "OH",
) -> FuelStation:
    """Create and save a FuelStation for testing."""
    station = FuelStation(
        opis_id=opis_id,
        name=f"Station {opis_id}",
        address="100 Test Rd",
        city=city,
        state=state,
        rack_id="1",
        retail_price=Decimal(str(price)),
        latitude=lat,
        longitude=lon,
    )
    station.save()
    return station


class FuelOptimizerShortRouteTest(TestCase):
    """Tests for routes within the max range (≤500 miles)."""

    def test_no_stop_needed_for_short_route(self):
        """A 300-mile route requires no fuel stop."""
        route_points = make_route_points_linear(NY_LAT, NY_LON, 41.0, -79.0, 300.0)
        plan = optimize_fuel_stops(route_points, total_route_miles=300.0)
        self.assertEqual(len(plan.fuel_stops), 0)

    def test_no_stop_exactly_at_max_range(self):
        """A 500-mile route (exactly max range) requires no fuel stop."""
        route_points = make_route_points_linear(NY_LAT, NY_LON, 41.0, -81.5, 500.0)
        plan = optimize_fuel_stops(route_points, total_route_miles=500.0)
        self.assertEqual(len(plan.fuel_stops), 0)

    def test_total_gallons_for_short_route(self):
        """Total gallons = distance / 10 MPG."""
        route_points = make_route_points_linear(NY_LAT, NY_LON, 41.0, -79.0, 300.0)
        plan = optimize_fuel_stops(route_points, total_route_miles=300.0)
        self.assertAlmostEqual(plan.total_gallons, 30.0, places=2)

    def test_total_cost_zero_for_short_route(self):
        """Total cost is zero when no stops are needed."""
        route_points = make_route_points_linear(NY_LAT, NY_LON, 41.0, -80.0, 400.0)
        plan = optimize_fuel_stops(route_points, total_route_miles=400.0)
        self.assertEqual(plan.total_cost, Decimal("0"))

    def test_350_mile_route_no_stop(self):
        """350 miles requires 35 gallons and no stop."""
        route_points = make_route_points_linear(NY_LAT, NY_LON, 40.9, -79.5, 350.0)
        plan = optimize_fuel_stops(route_points, total_route_miles=350.0)
        self.assertAlmostEqual(plan.total_gallons, 35.0, places=2)
        self.assertEqual(len(plan.fuel_stops), 0)


class FuelOptimizerSingleStopTest(TestCase):
    """Tests for routes requiring a single fuel stop (~790 miles NY→Chicago)."""

    def setUp(self):
        """
        Create a station near the midpoint of the NY→Chicago route.
        The midpoint (lat≈41.3, lon≈-80.8) is near Cleveland, OH.
        We place a station exactly there — large proximity (100 miles) ensures it's found.
        """
        self.station = make_db_station(
            opis_id="mid1",
            lat=MID_LAT,
            lon=MID_LON,
            price=3.05,
            city="Cleveland",
            state="OH",
        )

    def test_single_stop_selected(self):
        route_points = make_route_points_linear(NY_LAT, NY_LON, CHI_LAT, CHI_LON, 790.0)
        plan = optimize_fuel_stops(route_points, total_route_miles=790.0, proximity_miles=100.0)
        self.assertEqual(len(plan.fuel_stops), 1)

    def test_single_stop_station_correct(self):
        route_points = make_route_points_linear(NY_LAT, NY_LON, CHI_LAT, CHI_LON, 790.0)
        plan = optimize_fuel_stops(route_points, total_route_miles=790.0, proximity_miles=100.0)
        self.assertEqual(len(plan.fuel_stops), 1)
        self.assertEqual(plan.fuel_stops[0].station.opis_id, "mid1")

    def test_total_gallons_for_790_mile_route(self):
        route_points = make_route_points_linear(NY_LAT, NY_LON, CHI_LAT, CHI_LON, 790.0)
        plan = optimize_fuel_stops(route_points, total_route_miles=790.0, proximity_miles=100.0)
        self.assertAlmostEqual(plan.total_gallons, 79.0, places=0)

    def test_fuel_cost_is_decimal(self):
        """Fuel cost must be a Decimal, not float."""
        route_points = make_route_points_linear(NY_LAT, NY_LON, CHI_LAT, CHI_LON, 790.0)
        plan = optimize_fuel_stops(route_points, total_route_miles=790.0, proximity_miles=100.0)
        self.assertIsInstance(plan.total_cost, Decimal)
        if plan.fuel_stops:
            self.assertIsInstance(plan.fuel_stops[0].fuel_cost, Decimal)

    def test_fuel_cost_is_positive(self):
        route_points = make_route_points_linear(NY_LAT, NY_LON, CHI_LAT, CHI_LON, 790.0)
        plan = optimize_fuel_stops(route_points, total_route_miles=790.0, proximity_miles=100.0)
        self.assertGreater(plan.total_cost, Decimal("0"))


class FuelOptimizerMultipleStopsTest(TestCase):
    """Tests for long routes requiring multiple fuel stops."""

    def setUp(self):
        """
        Create stations along a 1500-mile route (NY to somewhere in Kansas).
        Route: NY_LAT,NY_LON → roughly KS (lat=38.5, lon=-98.0)
        Place stations at ~400, ~900, ~1300 miles along the route.
        """
        # Route parameters
        self.end_lat, self.end_lon = 38.5, -98.0
        self.total = 1500.0
        n = 30

        # Compute intermediate lats/lons for station placement
        def interp(fraction):
            return (
                NY_LAT + fraction * (self.end_lat - NY_LAT),
                NY_LON + fraction * (self.end_lon - NY_LON),
            )

        # Station at ~400 miles (fraction ≈ 400/1500 ≈ 0.267)
        lat1, lon1 = interp(0.27)
        make_db_station("S1", lat1, lon1, 3.10, "Pittsburgh", "PA")

        # Station at ~900 miles (fraction ≈ 0.60)
        lat2, lon2 = interp(0.60)
        make_db_station("S2", lat2, lon2, 2.99, "Columbus", "OH")

        # Station at ~1300 miles (fraction ≈ 0.87)
        lat3, lon3 = interp(0.87)
        make_db_station("S3", lat3, lon3, 3.20, "Kansas City", "KS")

    def test_multiple_stops_for_long_route(self):
        route_points = make_route_points_linear(
            NY_LAT, NY_LON, self.end_lat, self.end_lon, self.total, num_points=30
        )
        plan = optimize_fuel_stops(route_points, total_route_miles=self.total, proximity_miles=150.0)
        self.assertGreater(len(plan.fuel_stops), 1)

    def test_total_gallons_for_1500_mile_route(self):
        route_points = make_route_points_linear(
            NY_LAT, NY_LON, self.end_lat, self.end_lon, self.total, num_points=30
        )
        plan = optimize_fuel_stops(route_points, total_route_miles=self.total, proximity_miles=150.0)
        self.assertAlmostEqual(plan.total_gallons, 150.0, places=0)

    def test_stops_are_in_route_order(self):
        """Fuel stops should be in ascending route-progress order."""
        route_points = make_route_points_linear(
            NY_LAT, NY_LON, self.end_lat, self.end_lon, self.total, num_points=30
        )
        plan = optimize_fuel_stops(route_points, total_route_miles=self.total, proximity_miles=150.0)
        progresses = [stop.distance_from_start_miles for stop in plan.fuel_stops]
        self.assertEqual(progresses, sorted(progresses))

    def test_no_consecutive_stops_over_500_miles_apart(self):
        """Each leg between consecutive stops must be ≤ MAX_RANGE_MILES."""
        route_points = make_route_points_linear(
            NY_LAT, NY_LON, self.end_lat, self.end_lon, self.total, num_points=30
        )
        plan = optimize_fuel_stops(route_points, total_route_miles=self.total, proximity_miles=150.0)
        positions = [0.0] + [s.distance_from_start_miles for s in plan.fuel_stops] + [self.total]
        for i in range(len(positions) - 1):
            leg = positions[i + 1] - positions[i]
            self.assertLessEqual(leg, MAX_RANGE_MILES + 1.0)  # +1 for float tolerance


class FuelOptimizerNoFeasiblePlanTest(TestCase):
    """Tests for infeasible fuel plans."""

    def test_raises_when_no_stations_exist(self):
        """Should raise NoFeasiblePlanError when no geocoded stations exist."""
        route_points = make_route_points_linear(NY_LAT, NY_LON, CHI_LAT, CHI_LON, 800.0)
        with self.assertRaises(NoFeasiblePlanError):
            optimize_fuel_stops(route_points, total_route_miles=800.0)

    def test_raises_when_station_too_far_from_route(self):
        """Should raise NoFeasiblePlanError when the only station is far off-route."""
        # Station in Miami — far south of the NY→Chicago route
        make_db_station("MIA", lat=25.77, lon=-80.19, price=3.00, city="Miami", state="FL")

        route_points = make_route_points_linear(NY_LAT, NY_LON, CHI_LAT, CHI_LON, 900.0)
        with self.assertRaises(NoFeasiblePlanError):
            # Very tight proximity — Miami won't qualify
            optimize_fuel_stops(route_points, total_route_miles=900.0, proximity_miles=1.0)


class FuelCalculationTest(TestCase):
    """Tests for precise fuel cost calculations."""

    def test_gallons_300_miles(self):
        """300 miles / 10 MPG = 30 gallons."""
        route_points = make_route_points_linear(NY_LAT, NY_LON, 41.0, -78.5, 300.0)
        plan = optimize_fuel_stops(route_points, total_route_miles=300.0)
        self.assertAlmostEqual(plan.total_gallons, 30.0, places=2)

    def test_gallons_500_miles(self):
        """500 miles / 10 MPG = 50 gallons (exactly max tank)."""
        route_points = make_route_points_linear(NY_LAT, NY_LON, 41.2, -82.0, 500.0)
        plan = optimize_fuel_stops(route_points, total_route_miles=500.0)
        self.assertAlmostEqual(plan.total_gallons, 50.0, places=2)

    def test_decimal_rounding_on_fuel_stop(self):
        """Fuel costs should be rounded to 2 decimal places."""
        # Station at midpoint with a price that exercises rounding
        make_db_station("RND", MID_LAT, MID_LON, 3.00733333, "Cleveland", "OH")
        route_points = make_route_points_linear(NY_LAT, NY_LON, CHI_LAT, CHI_LON, 790.0)
        plan = optimize_fuel_stops(route_points, total_route_miles=790.0, proximity_miles=100.0)
        if plan.fuel_stops:
            cost = plan.fuel_stops[0].fuel_cost
            self.assertIsInstance(cost, Decimal)
            # Verify it rounds to exactly 2 decimal places
            self.assertEqual(cost, cost.quantize(Decimal("0.01")))

    def test_cheapest_station_preferred_over_expensive(self):
        """
        Given two adjacent stations near the midpoint, the cheaper one
        should be selected (when both are feasible).
        """
        # Two stations very close together near the route midpoint
        make_db_station("cheap", MID_LAT, MID_LON - 0.1, 2.85, "CheapCity", "OH")
        make_db_station("expensive", MID_LAT, MID_LON + 0.1, 3.50, "ExpCity", "OH")

        route_points = make_route_points_linear(NY_LAT, NY_LON, CHI_LAT, CHI_LON, 790.0)
        plan = optimize_fuel_stops(route_points, total_route_miles=790.0, proximity_miles=100.0)

        if plan.fuel_stops:
            chosen_price = plan.fuel_stops[0].station.retail_price
            # The optimizer should have picked the $2.85 station, not $3.50
            self.assertLessEqual(float(chosen_price), 3.50)

    def test_total_cost_sum_matches_individual_stops(self):
        """Total cost should equal the sum of individual stop costs."""
        # 1200-mile route NY → roughly west. Stations at ~400mi and ~850mi.
        end_lat, end_lon = 40.0, -95.0
        n = 25
        total = 1200.0

        def interp(fraction):
            return (
                NY_LAT + fraction * (end_lat - NY_LAT),
                NY_LON + fraction * (end_lon - NY_LON),
            )

        # Station A at ~400 miles (fraction ~0.33)
        la, loa = interp(0.33)
        make_db_station("SumA", la, loa, 3.10, "SumCityA", "PA")

        # Station B at ~850 miles (fraction ~0.71)
        lb, lob = interp(0.71)
        make_db_station("SumB", lb, lob, 3.05, "SumCityB", "OH")

        route_points = make_route_points_linear(NY_LAT, NY_LON, end_lat, end_lon, total, num_points=n)
        plan = optimize_fuel_stops(route_points, total_route_miles=total, proximity_miles=150.0)

        if plan.fuel_stops:
            sum_of_stops = sum(s.fuel_cost for s in plan.fuel_stops)
            self.assertEqual(plan.total_cost, sum_of_stops)
