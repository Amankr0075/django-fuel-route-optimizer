"""
Route geometry utility functions.

Provides:
- Haversine distance calculation between two lat/lon points.
- Perpendicular (cross-track) distance from a point to a route segment.
- Route progress (along-track distance) calculation.
- Fuel station filtering based on route proximity.
"""
import math
from dataclasses import dataclass
from typing import Sequence

# Earth radius in miles
_EARTH_RADIUS_MILES = 3_958.8


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate the great-circle distance between two points in miles.

    Uses the Haversine formula for spherical geometry.

    Args:
        lat1, lon1: Start point (degrees).
        lat2, lon2: End point (degrees).

    Returns:
        Distance in miles.
    """
    lat1_r = math.radians(lat1)
    lat2_r = math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)

    a = math.sin(dlat / 2) ** 2 + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return _EARTH_RADIUS_MILES * c


def cross_track_distance_miles(
    point_lat: float,
    point_lon: float,
    segment_start_lat: float,
    segment_start_lon: float,
    segment_end_lat: float,
    segment_end_lon: float,
) -> float:
    """
    Calculate the cross-track (perpendicular) distance from a point to a
    great-circle segment in miles.

    This is the minimum distance from the point to the infinite great-circle
    line passing through the segment.

    Args:
        point_lat, point_lon: The point to measure from.
        segment_start_*: Start of the segment.
        segment_end_*: End of the segment.

    Returns:
        Cross-track distance in miles (always non-negative).
    """
    R = _EARTH_RADIUS_MILES

    p_lat_r = math.radians(point_lat)
    p_lon_r = math.radians(point_lon)
    s_lat_r = math.radians(segment_start_lat)
    s_lon_r = math.radians(segment_start_lon)
    e_lat_r = math.radians(segment_end_lat)
    e_lon_r = math.radians(segment_end_lon)

    # Angular distance from segment start to point
    d13 = haversine_miles(segment_start_lat, segment_start_lon, point_lat, point_lon) / R

    # Bearing from segment start to point
    theta13 = math.atan2(
        math.sin(p_lon_r - s_lon_r) * math.cos(p_lat_r),
        math.cos(s_lat_r) * math.sin(p_lat_r)
        - math.sin(s_lat_r) * math.cos(p_lat_r) * math.cos(p_lon_r - s_lon_r),
    )

    # Bearing from segment start to segment end
    theta12 = math.atan2(
        math.sin(e_lon_r - s_lon_r) * math.cos(e_lat_r),
        math.cos(s_lat_r) * math.sin(e_lat_r)
        - math.sin(s_lat_r) * math.cos(e_lat_r) * math.cos(e_lon_r - s_lon_r),
    )

    # Cross-track distance
    dxt = math.asin(math.sin(d13) * math.sin(theta13 - theta12))
    return abs(dxt) * R


def along_track_distance_miles(
    point_lat: float,
    point_lon: float,
    segment_start_lat: float,
    segment_start_lon: float,
    segment_end_lat: float,
    segment_end_lon: float,
) -> float:
    """
    Calculate the along-track distance from the segment start to the closest
    point on the segment to the given point, in miles.

    Negative value means the closest point is behind the segment start.

    Args:
        point_lat, point_lon: The query point.
        segment_start_*: Start of the segment.
        segment_end_*: End of the segment.

    Returns:
        Along-track distance in miles.
    """
    R = _EARTH_RADIUS_MILES

    p_lat_r = math.radians(point_lat)
    p_lon_r = math.radians(point_lon)
    s_lat_r = math.radians(segment_start_lat)
    s_lon_r = math.radians(segment_start_lon)
    e_lat_r = math.radians(segment_end_lat)
    e_lon_r = math.radians(segment_end_lon)

    d13 = haversine_miles(segment_start_lat, segment_start_lon, point_lat, point_lon) / R

    theta13 = math.atan2(
        math.sin(p_lon_r - s_lon_r) * math.cos(p_lat_r),
        math.cos(s_lat_r) * math.sin(p_lat_r)
        - math.sin(s_lat_r) * math.cos(p_lat_r) * math.cos(p_lon_r - s_lon_r),
    )
    theta12 = math.atan2(
        math.sin(e_lon_r - s_lon_r) * math.cos(e_lat_r),
        math.cos(s_lat_r) * math.sin(e_lat_r)
        - math.sin(s_lat_r) * math.cos(e_lat_r) * math.cos(e_lon_r - s_lon_r),
    )

    dxt_r = math.asin(math.sin(d13) * math.sin(theta13 - theta12))
    dat = math.acos(math.cos(d13) / math.cos(dxt_r))
    # Sign: positive if in front of segment start
    sign = 1.0 if math.cos(theta13 - theta12) >= 0 else -1.0
    return sign * dat * R


@dataclass(frozen=True)
class RoutePoint:
    """A single coordinate along the route with cumulative distance metadata."""

    lat: float
    lon: float
    cumulative_miles: float  # Distance from the route start to this point


def build_route_points(
    coordinates: list[list[float]],
) -> list[RoutePoint]:
    """
    Convert a GeoJSON LineString coordinate list into RoutePoints with
    cumulative distances.

    GeoJSON coordinates are [lon, lat], so we reverse them.

    Args:
        coordinates: List of [longitude, latitude] pairs from GeoJSON.

    Returns:
        List of RoutePoint objects with cumulative distance from start.
    """
    if not coordinates:
        return []

    points: list[RoutePoint] = []
    cumulative = 0.0

    for i, coord in enumerate(coordinates):
        lon, lat = coord[0], coord[1]
        if i == 0:
            cumulative = 0.0
        else:
            prev = coordinates[i - 1]
            prev_lat, prev_lon = prev[1], prev[0]
            cumulative += haversine_miles(prev_lat, prev_lon, lat, lon)
        points.append(RoutePoint(lat=lat, lon=lon, cumulative_miles=cumulative))

    return points


def station_route_progress(
    station_lat: float,
    station_lon: float,
    route_points: list[RoutePoint],
    max_perpendicular_miles: float,
) -> tuple[float, float] | None:
    """
    Find a fuel station's position along the route.

    Iterates over route segments and finds the one with the minimum
    perpendicular distance to the station.  Returns the station's estimated
    cumulative distance from the start and its perpendicular distance.

    Args:
        station_lat: Station latitude.
        station_lon: Station longitude.
        route_points: RoutePoint list from build_route_points().
        max_perpendicular_miles: Maximum allowed perpendicular distance.

    Returns:
        (progress_miles, perp_miles) or None if the station is too far
        from any route segment.
    """
    if len(route_points) < 2:
        return None

    best_progress: float | None = None
    best_perp: float = float("inf")

    for i in range(len(route_points) - 1):
        seg_start = route_points[i]
        seg_end = route_points[i + 1]

        # Perpendicular distance from station to this segment
        perp = cross_track_distance_miles(
            station_lat,
            station_lon,
            seg_start.lat,
            seg_start.lon,
            seg_end.lat,
            seg_end.lon,
        )

        if perp < best_perp:
            # Along-track distance from this segment's start
            along = along_track_distance_miles(
                station_lat,
                station_lon,
                seg_start.lat,
                seg_start.lon,
                seg_end.lat,
                seg_end.lon,
            )
            seg_length = haversine_miles(
                seg_start.lat, seg_start.lon, seg_end.lat, seg_end.lon
            )

            # Clamp: if station projects behind the start or past the end,
            # use the segment endpoint.
            along_clamped = max(0.0, min(along, seg_length))
            progress = seg_start.cumulative_miles + along_clamped

            best_perp = perp
            best_progress = progress

    if best_progress is None or best_perp > max_perpendicular_miles:
        return None

    return (best_progress, best_perp)


def meters_to_miles(meters: float) -> float:
    """Convert metres to miles."""
    return meters / 1_609.344


def seconds_to_minutes(seconds: float) -> float:
    """Convert seconds to minutes."""
    return seconds / 60.0


def decode_polyline(encoded: str) -> list[list[float]]:
    """
    Decode a Google Encoded Polyline string into a list of [lon, lat] pairs
    (GeoJSON order).

    Used when routing APIs return encoded polylines instead of GeoJSON.

    Args:
        encoded: Google encoded polyline string.

    Returns:
        List of [longitude, latitude] coordinate pairs.
    """
    coords: list[list[float]] = []
    index = 0
    length = len(encoded)
    lat = 0
    lng = 0

    while index < length:
        # Latitude
        result = 0
        shift = 0
        while True:
            b = ord(encoded[index]) - 63
            index += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        dlat = ~(result >> 1) if result & 1 else result >> 1
        lat += dlat

        # Longitude
        result = 0
        shift = 0
        while True:
            b = ord(encoded[index]) - 63
            index += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        dlng = ~(result >> 1) if result & 1 else result >> 1
        lng += dlng

        # GeoJSON order: [lon, lat]
        coords.append([lng / 1e5, lat / 1e5])

    return coords
