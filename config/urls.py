"""
Root URL configuration for Fuel Route API.
"""
from django.contrib import admin
from django.http import JsonResponse
from django.urls import path, include


def api_root(request):
    """Root endpoint — returns API info and available endpoints."""
    return JsonResponse({
        "service": "Fuel Route API",
        "version": "1.0.0",
        "description": "Cost-optimised fuel stop planning for USA driving routes.",
        "endpoints": {
            "health": "GET /api/v1/health/",
            "route":  "POST /api/v1/route/",
        },
        "docs": "See README.md or import postman/fuel-route-api.postman_collection.json",
    })


urlpatterns = [
    path("", api_root),
    path("admin/", admin.site.urls),
    path("api/v1/", include("routing.urls")),
]
