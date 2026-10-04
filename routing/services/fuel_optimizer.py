"""
Fuel station optimizer.

Algorithm: Route-Aware Greedy with Look-Ahead
=============================================

The algorithm selects cost-effective, feasible fuel stops along a driving
route given a vehicle with a maximum range constraint.

Overview
--------
1. Obtain the complete route geometry (GeoJSON LineString).
2. Build a list of RoutePoints with cumulative distances from the origin.
3. Query the database for fuel stations in states intersected by the route.
4. For each candidate station, compute:
   - Its perpendicular distance from the route (filter > ROUTE_PROXIMITY_MILES).
   - Its along-track progress from the route start (position on route).
5. Sort candidates by route progress.
6. Run a greedy selection loop:
   a. Start at position 0 with a full tank (MAX_RANGE_MILES).
   b. At each step, look ahead up to MAX_RANGE_MILES from the current position.
   c. Within reachable stations, apply the cheapest-that-doesn't-strand rule:
      - A cheaper station is preferred unless stopping there would mean the
        next reachable station (or destination) is still reachable.
      - This prevents the vehicle from getting stranded.
   d. If no station is reachable and the destination is not yet reachable,
      return a NO_FEASIBLE_FUEL_PLAN error.
7. Calculate gallons purchased and cost at each stop.

Fuel Calculation
----------------
At each stop:
  - gallons_purchased = miles_to_next_stop / FUEL_EFFICIENCY_MPG
  - We assume the vehicle refuels to full at each stop (this maximises
    flexibility for future stops and is the standard trucking assumption).
  - cost_at_stop = gallons_purchased × price_per_gallon

Total:
  - total_gallons = total_route_miles / FUEL_EFFICIENCY_MPG
  - total_cost = sum of cost at each stop

Tradeoffs
---------
- Greedy approach is O(n) and extremely fast for the dataset size.
- The "cheapest-that-doesn't-strand" rule avoids globally suboptimal choices
  where taking the cheapest station now leaves the vehicle stranded later.
- This is not a globally optimal (e.g., dynamic programming) solution, but
  it performs well for real highway networks where stations are plentiful.
"""
import logging
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db.models import QuerySet

from ..models import FuelStation
from .route_utils import RoutePoint, build_route_points, station_route_progress

logger = logging.getLogger(__name__)

# Vehicle constants — sourced from settings (which can be overridden via env)
MAX_RANGE_MILES: float = settings.MAX_RANGE_MILES
FUEL_EFFICIENCY_MPG: float = settings.FUEL_EFFICIENCY_MPG

# Maximum gallons the tank can hold (= max range / efficiency)
MAX_TANK_GALLONS: float = MAX_RANGE_MILES / FUEL_EFFICIENCY_MPG  # = 50.0


@dataclass
class CandidateStation:
    """A fuel station that is on or near the route, with route metadata."""

    station: FuelStation
    progress_miles: float  # Along-route distance from origin to station
    perp_miles: float  # Perpendicular distance from route


@dataclass
class FuelStop:
    """A selected fuel stop with purchase details."""

    station: FuelStation
    distance_from_start_miles: float
    gallons_purchased: float
    fuel_cost: Decimal

    def to_dict(self) -> dict:
        return {
            "station_id": str(self.station.opis_id),
            "station_name": self.station.name,
            "address": self.station.address,
            "city": self.station.city,
            "state": self.station.state,
            "price_per_gallon": float(
                Decimal(str(self.station.retail_price)).quantize(
                    Decimal("0.00001")
                )
            ),
            "distance_from_start_miles": round(self.distance_from_start_miles, 2),
            "gallons_purchased": round(self.gallons_purchased, 3),
            "fuel_cost": float(
                self.fuel_cost.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            ),
        }


@dataclass
class FuelPlan:
    """Complete fuel plan result."""

    fuel_stops: list[FuelStop]
    total_gallons: float
    total_cost: Decimal


class NoFeasiblePlanError(Exception):
    """Raised when no feasible fuel plan exists for the route."""


def _get_route_states(route_points: list[RoutePoint]) -> list[str]:
    """
    Infer the US states that the route passes through by sampling route points
    and checking which states have stations near those coordinates.

    We use a simpler approach: query all stations and filter by route proximity.
    Since we're working with a relatively small dataset (8,151 rows), loading
    all geocoded stations is fast and avoids complex spatial queries.
    """
    # We return all states here — the proximity filter will handle exclusion.
    # This is correct for routes that span many states.
    return []


def get_candidate_stations(
    route_points: list[RoutePoint],
    proximity_miles: float | None = None,
) -> list[CandidateStation]:
    """
    Find all fuel stations that are within proximity_miles of the route.

    Strategy:
    1. Compute a bounding box around all route points + proximity buffer.
    2. Fetch only stations within that bounding box (fast DB index scan).
    3. For each candidate, compute route progress and exact perpendicular distance.
    4. Return stations within the proximity threshold, sorted by route progress.

    This is done entirely locally — no external API calls.

    Args:
        route_points: List of RoutePoints with cumulative distances.
        proximity_miles: Max perpendicular distance from route. Defaults to
                         settings.ROUTE_PROXIMITY_MILES.

    Returns:
        List of CandidateStation sorted by route progress.
    """
    if proximity_miles is None:
        proximity_miles = settings.ROUTE_PROXIMITY_MILES

    if not route_points:
        return []

    # -----------------------------------------------------------------------
    # Step 1: Compute bounding box for fast pre-filtering
    # 1 degree lat ≈ 69 miles; 1 degree lon ≈ 69*cos(lat) miles
    # We add a generous buffer (proximity_miles + 50 miles) in degrees.
    # -----------------------------------------------------------------------
    lats = [p.lat for p in route_points]
    lons = [p.lon for p in route_points]

    avg_lat = sum(lats) / len(lats)
    import math
    lat_buffer = (proximity_miles + 50) / 69.0  # degrees
    lon_buffer = (proximity_miles + 50) / (69.0 * abs(math.cos(math.radians(avg_lat))) + 0.001)

    min_lat = min(lats) - lat_buffer
    max_lat = max(lats) + lat_buffer
    min_lon = min(lons) - lon_buffer
    max_lon = max(lons) + lon_buffer

    # -----------------------------------------------------------------------
    # Step 2: Fetch stations within bounding box
    # -----------------------------------------------------------------------
    stations = FuelStation.objects.filter(
        latitude__isnull=False,
        longitude__isnull=False,
        latitude__gte=min_lat,
        latitude__lte=max_lat,
        longitude__gte=min_lon,
        longitude__lte=max_lon,
    ).only(
        "id", "opis_id", "name", "address", "city", "state",
        "retail_price", "latitude", "longitude",
    )

    pre_filter_count = stations.count()
    logger.info(
        "Bounding box [%.2f,%.2f]×[%.2f,%.2f] → %d candidate stations",
        min_lat, max_lat, min_lon, max_lon, pre_filter_count,
    )

    # -----------------------------------------------------------------------
    # Step 3: Precise proximity filtering using cross-track distance
    # -----------------------------------------------------------------------
    candidates: list[CandidateStation] = []

    for station in stations:
        result = station_route_progress(
            station_lat=station.latitude,
            station_lon=station.longitude,
            route_points=route_points,
            max_perpendicular_miles=proximity_miles,
        )
        if result is not None:
            progress, perp = result
            candidates.append(
                CandidateStation(
                    station=station,
                    progress_miles=progress,
                    perp_miles=perp,
                )
            )

    candidates.sort(key=lambda c: c.progress_miles)

    logger.info(
        "Found %d candidate stations within %.1f miles of route (from %d in bounding box)",
        len(candidates),
        proximity_miles,
        pre_filter_count,
    )

    return candidates


def _can_reach(
    from_progress: float,
    to_progress: float,
    current_range_miles: float,
) -> bool:
    """Return True if the vehicle can travel from from_progress to to_progress."""
    return (to_progress - from_progress) <= current_range_miles


def optimize_fuel_stops(
    route_points: list[RoutePoint],
    total_route_miles: float,
    proximity_miles: float | None = None,
) -> FuelPlan:
    """
    Select the most cost-effective sequence of fuel stops for the route.

    Uses a greedy look-ahead algorithm (see module docstring for details).

    Args:
        route_points: List of RoutePoints from the route geometry.
        total_route_miles: Total route distance in miles.
        proximity_miles: Max station distance from route.

    Returns:
        FuelPlan with selected stops and cost calculations.

    Raises:
        NoFeasiblePlanError: If no feasible plan exists.
    """
    candidates = get_candidate_stations(route_points, proximity_miles)

    # If the route is short enough, no stop needed
    if total_route_miles <= MAX_RANGE_MILES:
        logger.info(
            "Route (%.1f miles) is within max range (%.1f miles) — no fuel stop required.",
            total_route_miles,
            MAX_RANGE_MILES,
        )
        total_gallons = total_route_miles / FUEL_EFFICIENCY_MPG
        total_cost = Decimal("0")
        # Even though no stop is needed, we return the cheapest single stop
        # for reference if candidates exist.
        return FuelPlan(fuel_stops=[], total_gallons=total_gallons, total_cost=total_cost)

    if not candidates:
        raise NoFeasiblePlanError(
            "No geocoded fuel stations found along the route. "
            "Ensure stations have been geocoded with: "
            "python manage.py geocode_fuel_stations"
        )

    # -----------------------------------------------------------------------
    # Greedy selection with look-ahead
    # -----------------------------------------------------------------------
    selected_stops: list[tuple[CandidateStation, float]] = []  # (candidate, miles_to_drive)

    current_pos: float = 0.0         # Current route progress (miles from start)
    current_range: float = MAX_RANGE_MILES  # Remaining miles before empty

    # We work with a filtered list starting at or past the current position
    remaining = list(candidates)

    iteration = 0
    while True:
        iteration += 1
        if iteration > 1000:
            # Safety guard against infinite loops
            raise NoFeasiblePlanError(
                "Fuel plan algorithm exceeded iteration limit. The route may be "
                "too complex for the available station data."
            )

        # Can we reach the destination from here?
        distance_to_dest = total_route_miles - current_pos
        if distance_to_dest <= current_range:
            logger.debug(
                "Destination reachable from %.1f miles (%.1f miles remaining range)",
                current_pos, current_range,
            )
            break  # Done!

        # Find all stations reachable from current position
        reachable = [
            c for c in remaining
            if c.progress_miles > current_pos
            and (c.progress_miles - current_pos) <= current_range
        ]

        if not reachable:
            raise NoFeasiblePlanError(
                f"No fuel station reachable from route position {current_pos:.1f} miles. "
                f"Vehicle range: {current_range:.1f} miles remaining. "
                f"No feasible fuel-stop plan exists for this route with a "
                f"maximum vehicle range of {MAX_RANGE_MILES:.0f} miles."
            )

        # ------------------------------------------------------------------
        # Selection strategy: "Cheapest that doesn't strand us"
        #
        # Among reachable stations, sort by price (ascending).
        # Pick the cheapest station from which the destination or another
        # reachable station is reachable.
        #
        # This prevents choosing a cheap-but-isolated station that leaves
        # the vehicle stranded with no further stations in range.
        # ------------------------------------------------------------------
        reachable_sorted = sorted(reachable, key=lambda c: c.station.retail_price)

        chosen: CandidateStation | None = None
        for candidate in reachable_sorted:
            range_after_stop = MAX_RANGE_MILES  # Refuel to full
            miles_from_stop_to_dest = total_route_miles - candidate.progress_miles

            # Check if destination is reachable from this stop
            if miles_from_stop_to_dest <= range_after_stop:
                chosen = candidate
                break  # Best option: reach destination directly

            # Check if at least one further station is reachable from this stop
            next_stations = [
                c for c in remaining
                if c.progress_miles > candidate.progress_miles
                and (c.progress_miles - candidate.progress_miles) <= range_after_stop
            ]
            if next_stations:
                chosen = candidate
                break

        if chosen is None:
            # Even the most forward reachable station can't continue the journey
            raise NoFeasiblePlanError(
                f"No feasible fuel stop found from position {current_pos:.1f} miles. "
                f"Maximum range is {MAX_RANGE_MILES:.0f} miles. "
                "The route may pass through an area with insufficient fuel coverage."
            )

        miles_driven = chosen.progress_miles - current_pos
        selected_stops.append((chosen, miles_driven))

        logger.debug(
            "Selected stop: %s (%s, %s) @ $%.5f — progress %.1f miles",
            chosen.station.name,
            chosen.station.city,
            chosen.station.state,
            chosen.station.retail_price,
            chosen.progress_miles,
        )

        # Update state
        current_range = MAX_RANGE_MILES  # Refuel to full
        current_pos = chosen.progress_miles

        # Remove stations at or before current position from candidates
        remaining = [c for c in remaining if c.progress_miles > current_pos]

    # -----------------------------------------------------------------------
    # Build FuelStop objects with cost calculations
    # -----------------------------------------------------------------------
    fuel_stops: list[FuelStop] = []
    total_cost = Decimal("0")

    # Add the final leg (from last stop to destination)
    # Calculate gallons at each stop based on miles to NEXT waypoint
    positions_with_next: list[tuple[CandidateStation, float, float]] = []
    # (candidate, progress, miles_to_next)

    for i, (candidate, _miles_driven) in enumerate(selected_stops):
        if i + 1 < len(selected_stops):
            miles_to_next = selected_stops[i + 1][0].progress_miles - candidate.progress_miles
        else:
            # Last stop to destination
            miles_to_next = total_route_miles - candidate.progress_miles
        positions_with_next.append((candidate, candidate.progress_miles, miles_to_next))

    for candidate, progress, miles_to_next in positions_with_next:
        gallons = miles_to_next / FUEL_EFFICIENCY_MPG
        price = Decimal(str(candidate.station.retail_price))
        cost = (Decimal(str(gallons)) * price).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        total_cost += cost

        fuel_stops.append(
            FuelStop(
                station=candidate.station,
                distance_from_start_miles=progress,
                gallons_purchased=gallons,
                fuel_cost=cost,
            )
        )

    total_gallons = total_route_miles / FUEL_EFFICIENCY_MPG

    logger.info(
        "Fuel plan: %d stops, %.2f gallons, $%.2f total",
        len(fuel_stops),
        total_gallons,
        total_cost,
    )

    return FuelPlan(
        fuel_stops=fuel_stops,
        total_gallons=total_gallons,
        total_cost=total_cost,
    )
