"""
Routing service using OSRM (Open Source Routing Machine).

Architecture:
- Uses the free public OSRM demo server (router.project-osrm.org).
- Sends a SINGLE route request per user API call.
- Returns route distance (miles), duration (minutes), and GeoJSON geometry.
- Results are cached using the start/end coordinates as cache keys.

OSRM Route API (v1):
    GET /route/v1/{profile}/{coordinates}
    https://project-osrm.org/docs/v5.24.0/api/

Key parameters:
    - overview=full  — returns the complete route geometry
    - geometries=geojson — returns geometry as GeoJSON (not encoded polyline)
    - steps=false — we don't need turn-by-turn instructions
"""
import hashlib
import json
import logging

import httpx
from django.conf import settings
from django.core.cache import cache

from .route_utils import meters_to_miles, seconds_to_minutes

logger = logging.getLogger(__name__)


class RoutingError(Exception):
    """Raised when OSRM routing fails."""


class NoRouteFoundError(RoutingError):
    """Raised when OSRM cannot find a route between the given points."""


class RouteResult:
    """
    Encapsulates the result of an OSRM routing request.

    Attributes:
        distance_miles: Total route distance in miles.
        duration_minutes: Estimated driving duration in minutes.
        geometry: GeoJSON LineString dict with full route geometry.
    """

    def __init__(
        self,
        distance_miles: float,
        duration_minutes: float,
        geometry: dict,
    ) -> None:
        self.distance_miles = distance_miles
        self.duration_minutes = duration_minutes
        self.geometry = geometry  # {"type": "LineString", "coordinates": [...]}

    def to_dict(self) -> dict:
        return {
            "distance_miles": round(self.distance_miles, 2),
            "duration_minutes": round(self.duration_minutes, 1),
            "geometry": self.geometry,
        }


def _make_cache_key(
    start_lat: float,
    start_lon: float,
    end_lat: float,
    end_lon: float,
) -> str:
    """Create a deterministic cache key for a route request."""
    key_str = f"route:{start_lat:.5f},{start_lon:.5f}:{end_lat:.5f},{end_lon:.5f}"
    return hashlib.md5(key_str.encode()).hexdigest()  # noqa: S324


def get_driving_route(
    start_lat: float,
    start_lon: float,
    end_lat: float,
    end_lon: float,
) -> RouteResult:
    """
    Calculate the driving route between two points using OSRM.

    Makes a SINGLE API request per unique start/end coordinate pair.
    Results are cached for 1 hour.

    Args:
        start_lat, start_lon: Start coordinates.
        end_lat, end_lon: End coordinates.

    Returns:
        RouteResult with distance, duration, and GeoJSON geometry.

    Raises:
        NoRouteFoundError: If no driving route exists.
        RoutingError: For API errors or network issues.
    """
    cache_key = _make_cache_key(start_lat, start_lon, end_lat, end_lon)
    cached = cache.get(cache_key)
    if cached:
        logger.debug(
            "Routing cache HIT for (%.4f,%.4f)→(%.4f,%.4f)",
            start_lat, start_lon, end_lat, end_lon,
        )
        return RouteResult(**cached)

    logger.info(
        "Routing cache MISS — calling OSRM for (%.4f,%.4f)→(%.4f,%.4f)",
        start_lat, start_lon, end_lat, end_lon,
    )

    # OSRM coordinate format: longitude,latitude (note order)
    coordinates = f"{start_lon},{start_lat};{end_lon},{end_lat}"
    url = f"{settings.OSRM_BASE_URL}/route/v1/driving/{coordinates}"
    params = {
        "overview": "full",
        "geometries": "geojson",
        "steps": "false",
        "annotations": "false",
    }

    try:
        with httpx.Client(timeout=settings.HTTP_TIMEOUT) as client:
            response = client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
    except httpx.TimeoutException as exc:
        logger.error("OSRM request timed out: %s", exc)
        raise RoutingError("Routing service timed out. Please try again.") from exc
    except httpx.HTTPStatusError as exc:
        logger.error("OSRM HTTP error: %s", exc)
        if exc.response.status_code == 429:
            raise RoutingError(
                "Routing service rate limit exceeded. Please try again later."
            ) from exc
        raise RoutingError(
            f"Routing service returned HTTP {exc.response.status_code}."
        ) from exc
    except Exception as exc:
        logger.error("Unexpected OSRM error: %s", exc)
        raise RoutingError(f"Unexpected error contacting routing service: {exc}") from exc

    # Parse OSRM response
    code = data.get("code", "")
    if code != "Ok":
        msg = data.get("message", "No route found.")
        logger.warning("OSRM returned non-Ok code '%s': %s", code, msg)
        if code in ("NoRoute", "NoSegment", "NoMatch"):
            raise NoRouteFoundError(
                f"No drivable route found between the provided locations. "
                f"OSRM code: {code}"
            )
        raise RoutingError(f"Routing service error: {msg} (code: {code})")

    routes = data.get("routes", [])
    if not routes:
        raise NoRouteFoundError("Routing service returned no routes.")

    route = routes[0]
    distance_meters: float = route.get("distance", 0.0)
    duration_seconds: float = route.get("duration", 0.0)
    geometry: dict = route.get("geometry", {})

    distance_miles = meters_to_miles(distance_meters)
    duration_minutes = seconds_to_minutes(duration_seconds)

    result = RouteResult(
        distance_miles=distance_miles,
        duration_minutes=duration_minutes,
        geometry=geometry,
    )

    # Cache route for 1 hour
    cache.set(cache_key, result.to_dict(), timeout=3_600)
    logger.info(
        "Route computed: %.1f miles, %.0f minutes",
        distance_miles, duration_minutes,
    )

    return result
