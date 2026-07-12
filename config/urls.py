"""Root URL configuration."""

from django.urls import include, path

from core.views import health, index

urlpatterns = [
    path("", index, name="index"),
    path("health", health, name="health"),
    path("healthz", health),  # common k8s-style alias
    path("ingest/", include("analytics_proxy.urls")),
]
