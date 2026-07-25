"""Root URL configuration."""

from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from core.views import favicon, favicon_full, health, index, landing

urlpatterns = [
    # The landing page owns the root; the JSON service descriptor moved to /api/.
    path("", landing, name="landing"),
    path("api/", index, name="index"),
    path("health", health, name="health"),
    path("healthz", health),  # common k8s-style alias
    path("favicon.svg", favicon),
    path("favicon.ico", favicon),  # browsers request this by default
    path("favicon-full.svg", favicon_full),  # the detailed mark, for app icons and avatars
    path("admin/", admin.site.urls),
    path("ingest/", include("analytics_proxy.urls")),
    path("chat/", include("chat.urls")),
    # OpenAPI schema (machine) + Swagger UI (human). The schema also feeds the
    # frontend's type generation.
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
]
