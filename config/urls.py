"""Root URL configuration."""

from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from core.views import (
    favicon,
    favicon_full,
    favicon_glyph,
    health,
    index,
    landing,
    me,
    signin,
)

urlpatterns = [
    # The landing page owns the root; the JSON service descriptor moved to /api/.
    path("", landing, name="landing"),
    path("api/", index, name="index"),
    path("api/me", me, name="me"),  # session check for the dashboard SPA's auth gate
    path("health", health, name="health"),
    path("healthz", health),  # common k8s-style alias
    path("favicon.svg", favicon),
    path("favicon.ico", favicon),  # browsers request this by default
    path("favicon-full.svg", favicon_full),  # the detailed mark, for app icons and avatars
    path("favicon-glyph.svg", favicon_glyph),  # tile-less, for CSS masking on a coloured rail
    path("signin", signin, name="signin"),  # social sign-in / sign-up (the CTAs land here)
    path("admin/", admin.site.urls),
    path("ingest/", include("analytics_proxy.urls")),
    path("chat/", include("chat.urls")),
    # Auth: allauth's provider callback endpoints (/accounts/<provider>/login/callback/,
    # which the registered OAuth apps point at) and the headless REST API the dashboard SPA
    # drives sign-in through (/_allauth/…).
    path("accounts/", include("allauth.urls")),
    path("_allauth/", include("allauth.headless.urls")),
    # OpenAPI schema (machine) + Swagger UI (human). The schema also feeds the
    # frontend's type generation.
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
]
