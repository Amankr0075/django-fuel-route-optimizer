"""
URL patterns for the routing app.
"""
from django.urls import path

from .views import HealthCheckView, RouteView

app_name = "routing"

urlpatterns = [
    path("health/", HealthCheckView.as_view(), name="health"),
    path("route/", RouteView.as_view(), name="route"),
]
