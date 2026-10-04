"""
API views for the Fuel Route API.

Endpoints:
    GET  /api/v1/health/   — Health check
    POST /api/v1/route/    — Calculate fuel-optimised route

Design principles:
- Views are thin orchestrators; all business logic lives in services.
- Errors are returned in a consistent envelope format.
- No stack traces are exposed to API clients.
"""
import logging
from decimal import Decimal

from django.conf import settings
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from .exceptions import error_response
from .models import FuelStation
from .serializers import RouteRequestSerializer
from .services.fuel_optimizer import (
    NoFeasiblePlanError,
    optimize_fuel_stops,
)
from .services.geocoding_service import (
    GeocodingError,
    LocationNotInUSAError,
    geocode_location,
)
from .services.route_utils import build_route_points
from .services.routing_service import (
    NoRouteFoundError,
    RoutingError,
    get_driving_route,
)

logger = logging.getLogger(__name__)


class HealthCheckView(APIView):
    """
    GET /api/v1/health/

    Returns service health status and basic statistics.
    """

    def get(self, request: Request) -> Response:
        station_count = FuelStation.objects.count()
        geocoded_count = FuelStation.objects.filter(
            latitude__isnull=False, longitude__isnull=False
        ).count()

        return Response(
            {
                "status": "ok",
                "service": "Fuel Route API",
                "version": "1.0.0",
                "database": {
                    "fuel_stations_total": station_count,
                    "fuel_stations_geocoded": geocoded_count,
                },
                "vehicle": {
                    "max_range_miles": settings.MAX_RANGE_MILES,
                    "fuel_efficiency_mpg": settings.FUEL_EFFICIENCY_MPG,
                },
                "routing_provider": "OSRM (router.project-osrm.org)",
                "geocoding_provider": "Nominatim (nominatim.openstreetmap.org)",
            },
            status=status.HTTP_200_OK,
        )


class RouteView(APIView):
    """
    POST /api/v1/route/

    Calculate a fuel-optimised driving route between two USA locations.

    Request body:
        {
            "start": "New York, NY",
            "finish": "Chicago, IL"
        }

    Response:
        Full route details including geometry, fuel stops, and cost breakdown.
    """

    def post(self, request: Request) -> Response:  # noqa: PLR0911, PLR0914
        """Handle route calculation request."""
        # ------------------------------------------------------------------
        # 1. Validate request body
        # ------------------------------------------------------------------
        serializer = RouteRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"error": {"code": "INVALID_REQUEST", "message": self._format_errors(serializer.errors)}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        start_input: str = serializer.validated_data["start"]
        finish_input: str = serializer.validated_data["finish"]

        logger.info("Route request: '%s' → '%s'", start_input, finish_input)

        # ------------------------------------------------------------------
        # 2. Geocode start and finish locations
        # ------------------------------------------------------------------
        try:
            start_geo = geocode_location(start_input)
        except LocationNotInUSAError as exc:
            return error_response(
                "LOCATION_NOT_IN_USA",
                str(exc),
                status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        except GeocodingError as exc:
            return error_response(
                "INVALID_LOCATION",
                f"Unable to resolve the start location: {exc}",
                status.HTTP_404_NOT_FOUND,
            )

        try:
            finish_geo = geocode_location(finish_input)
        except LocationNotInUSAError as exc:
            return error_response(
                "LOCATION_NOT_IN_USA",
                str(exc),
                status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        except GeocodingError as exc:
            return error_response(
                "INVALID_LOCATION",
                f"Unable to resolve the finish location: {exc}",
                status.HTTP_404_NOT_FOUND,
            )

        # ------------------------------------------------------------------
        # 3. Get driving route (ONE external API call)
        # ------------------------------------------------------------------
        try:
            route = get_driving_route(
                start_lat=start_geo.latitude,
                start_lon=start_geo.longitude,
                end_lat=finish_geo.latitude,
                end_lon=finish_geo.longitude,
            )
        except NoRouteFoundError as exc:
            return error_response(
                "NO_ROUTE_FOUND",
                str(exc),
                status.HTTP_404_NOT_FOUND,
            )
        except RoutingError as exc:
            logger.error("Routing error: %s", exc)
            return error_response(
                "ROUTING_SERVICE_ERROR",
                str(exc),
                status.HTTP_502_BAD_GATEWAY,
            )

        # ------------------------------------------------------------------
        # 4. Build route points and optimise fuel stops
        # ------------------------------------------------------------------
        coordinates = route.geometry.get("coordinates", [])
        route_points = build_route_points(coordinates)

        try:
            fuel_plan = optimize_fuel_stops(
                route_points=route_points,
                total_route_miles=route.distance_miles,
            )
        except NoFeasiblePlanError as exc:
            return error_response(
                "NO_FEASIBLE_FUEL_PLAN",
                str(exc),
                status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        except Exception as exc:
            logger.exception("Unexpected error during fuel optimisation: %s", exc)
            return error_response(
                "INTERNAL_ERROR",
                "An unexpected error occurred during fuel optimisation.",
                status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        # ------------------------------------------------------------------
        # 5. Build and return response
        # ------------------------------------------------------------------
        total_cost_rounded = fuel_plan.total_cost.quantize(Decimal("0.01"))

        response_data = {
            "start": {
                "input": start_input,
                "latitude": round(start_geo.latitude, 6),
                "longitude": round(start_geo.longitude, 6),
                "display_name": start_geo.display_name,
            },
            "finish": {
                "input": finish_input,
                "latitude": round(finish_geo.latitude, 6),
                "longitude": round(finish_geo.longitude, 6),
                "display_name": finish_geo.display_name,
            },
            "route": {
                "distance_miles": round(route.distance_miles, 2),
                "duration_minutes": round(route.duration_minutes, 1),
                "geometry": route.geometry,
            },
            "vehicle": {
                "max_range_miles": settings.MAX_RANGE_MILES,
                "fuel_efficiency_mpg": settings.FUEL_EFFICIENCY_MPG,
            },
            "fuel_stops": [stop.to_dict() for stop in fuel_plan.fuel_stops],
            "fuel": {
                "total_gallons": round(fuel_plan.total_gallons, 3),
                "total_cost": float(total_cost_rounded),
                "currency": "USD",
                "stop_count": len(fuel_plan.fuel_stops),
            },
        }

        logger.info(
            "Route complete: %.1f miles, %d stops, $%.2f total fuel cost",
            route.distance_miles,
            len(fuel_plan.fuel_stops),
            total_cost_rounded,
        )

        return Response(response_data, status=status.HTTP_200_OK)

    @staticmethod
    def _format_errors(errors: dict) -> str:
        """Format DRF serializer errors into a single human-readable string."""
        messages = []
        for field, errs in errors.items():
            if isinstance(errs, list):
                for err in errs:
                    if field == "non_field_errors":
                        messages.append(str(err))
                    else:
                        messages.append(f"{field}: {err}")
            else:
                messages.append(f"{field}: {errs}")
        return " | ".join(messages)
