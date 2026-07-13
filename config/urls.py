"""Root URL configuration."""

from django.contrib import admin
from django.urls import include, path

from core.views import favicon, health, index

urlpatterns = [
    path("", index, name="index"),
    path("health", health, name="health"),
    path("healthz", health),  # common k8s-style alias
    path("favicon.svg", favicon),
    path("favicon.ico", favicon),  # browsers request this by default
    path("admin/", admin.site.urls),
    path("ingest/", include("analytics_proxy.urls")),
    path("chat/", include("chat.urls")),
]
