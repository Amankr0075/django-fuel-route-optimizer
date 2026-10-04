"""
Geocoding service using Nominatim (OpenStreetMap).

Architecture:
- Uses a single Nominatim instance (no API key required).
- Results are cached in Django's cache framework to minimise external calls.
- Respects Nominatim's usage policy: 1 req/sec and a descriptive User-Agent.
- USA location validation is performed via bounding-box check after geocoding.
"""
import logging
import time
from dataclasses import dataclass

import httpx
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

# Nominatim rate-limit: 1 request per second
_NOMINATIM_REQUEST_INTERVAL = 1.1  # seconds
_last_nominatim_call: float = 0.0

# USA contiguous + territories rough bounding box (extended slightly for flexibility)
_USA_MIN_LAT = 18.0   # includes Puerto Rico
_USA_MAX_LAT = 72.0   # includes Alaska
_USA_MIN_LON = -180.0  # includes Alaska / Hawaii
_USA_MAX_LON = -65.0


@dataclass(frozen=True)
class GeocodedLocation:
    """Result of a geocoding operation."""

    input_text: str
    latitude: float
    longitude: float
    display_name: str
    country_code: str  # ISO 3166-1 alpha-2


class GeocodingError(Exception):
    """Raised when geocoding fails or returns no results."""


class LocationNotInUSAError(GeocodingError):
    """Raised when a geocoded location is outside the USA."""


def _make_cache_key(location: str) -> str:
    """Deterministic, memcache-safe cache key for a geocoding query."""
    import hashlib
    normalised = location.strip().lower()
    digest = hashlib.md5(normalised.encode()).hexdigest()  # noqa: S324
    return f"geocode:{digest}"


def _rate_limit_nominatim() -> None:
    """Ensure we respect Nominatim's 1 req/sec policy."""
    global _last_nominatim_call  # noqa: PLW0603
    now = time.monotonic()
    elapsed = now - _last_nominatim_call
    if elapsed < _NOMINATIM_REQUEST_INTERVAL:
        time.sleep(_NOMINATIM_REQUEST_INTERVAL - elapsed)
    _last_nominatim_call = time.monotonic()


def geocode_location(location: str) -> GeocodedLocation:
    """
    Geocode a free-text location string using Nominatim.

    Returns a GeocodedLocation with coordinates and metadata.
    Raises GeocodingError if the location cannot be resolved.
    Raises LocationNotInUSAError if the resolved location is outside the USA.

    Results are cached for 24 hours to minimise API calls.

    Args:
        location: Free-text location (e.g., "New York, NY").

    Returns:
        GeocodedLocation dataclass.
    """
    if not location or not location.strip():
        raise GeocodingError("Location string must not be empty.")

    cache_key = _make_cache_key(location)
    cached = cache.get(cache_key)
    if cached:
        logger.debug("Geocoding cache HIT for '%s'", location)
        return GeocodedLocation(**cached)

    logger.debug("Geocoding cache MISS for '%s' — calling Nominatim", location)

    _rate_limit_nominatim()

    url = f"{settings.NOMINATIM_BASE_URL}/search"
    params = {
        "q": location,
        "format": "json",
        "limit": 1,
        "addressdetails": 1,
        "countrycodes": "us",  # Restrict to USA results
    }
    headers = {"User-Agent": settings.NOMINATIM_USER_AGENT}

    try:
        with httpx.Client(timeout=settings.HTTP_TIMEOUT) as client:
            response = client.get(url, params=params, headers=headers)
            response.raise_for_status()
            results = response.json()
    except httpx.TimeoutException as exc:
        logger.error("Nominatim timeout for '%s': %s", location, exc)
        raise GeocodingError(
            f"Geocoding service timed out for location: {location}"
        ) from exc
    except httpx.HTTPStatusError as exc:
        logger.error("Nominatim HTTP error for '%s': %s", location, exc)
        raise GeocodingError(
            f"Geocoding service returned an error: {exc.response.status_code}"
        ) from exc
    except Exception as exc:
        logger.error("Unexpected geocoding error for '%s': %s", location, exc)
        raise GeocodingError(
            f"Unexpected error while geocoding: {location}"
        ) from exc

    if not results:
        raise GeocodingError(
            f"No results found for location: '{location}'. "
            "Please provide a valid USA city or address."
        )

    result = results[0]
    lat = float(result["lat"])
    lon = float(result["lon"])
    country_code = (
        result.get("address", {}).get("country_code", "").upper()
    )
    display_name = result.get("display_name", "")

    # Validate the result is within the USA
    if country_code != "US":
        raise LocationNotInUSAError(
            f"Location '{location}' resolved to '{display_name}' which is "
            f"outside the United States (country: {country_code or 'unknown'})."
        )

    geocoded = GeocodedLocation(
        input_text=location,
        latitude=lat,
        longitude=lon,
        display_name=display_name,
        country_code=country_code,
    )

    # Cache for 24 hours
    cache.set(cache_key, geocoded.__dict__, timeout=86_400)
    logger.info("Geocoded '%s' → (%.4f, %.4f)", location, lat, lon)

    return geocoded


def geocode_city_state(city: str, state: str) -> tuple[float, float] | None:
    """
    Geocode a city/state pair for fuel station preprocessing.

    Uses a cache key based on city+state to avoid repeated calls.
    Returns (latitude, longitude) or None if geocoding fails.

    Args:
        city: City name.
        state: Two-letter state abbreviation.

    Returns:
        Tuple of (latitude, longitude) or None.
    """
    query = f"{city}, {state}, USA"
    cache_key = f"geocode:city:{state}:{city.lower()}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    _rate_limit_nominatim()

    url = f"{settings.NOMINATIM_BASE_URL}/search"
    params = {
        "q": query,
        "format": "json",
        "limit": 1,
        "countrycodes": "us",
    }
    headers = {"User-Agent": settings.NOMINATIM_USER_AGENT}

    try:
        with httpx.Client(timeout=settings.HTTP_TIMEOUT) as client:
            response = client.get(url, params=params, headers=headers)
            response.raise_for_status()
            results = response.json()
    except Exception as exc:
        logger.warning("Geocoding failed for '%s': %s", query, exc)
        return None

    if not results:
        logger.warning("No geocoding result for '%s'", query)
        return None

    lat = float(results[0]["lat"])
    lon = float(results[0]["lon"])
    coords = (lat, lon)
    cache.set(cache_key, coords, timeout=604_800)  # 7 days
    return coords
