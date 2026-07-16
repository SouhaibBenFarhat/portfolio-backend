"""Basic service endpoints.

`/` and `/health` are DRF views so drf-spectacular documents them in the OpenAPI
schema. `favicon` stays a plain Django view — it serves an image, not JSON API.
"""

from django.http import HttpRequest, HttpResponse
from drf_spectacular.utils import extend_schema
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from .serializers import HealthSerializer, ServiceDescriptorSerializer

# A small SVG favicon: an "S" monogram on an indigo→violet gradient rounded square.
FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
    '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
    '<stop offset="0" stop-color="#6366f1"/><stop offset="1" stop-color="#8b5cf6"/>'
    "</linearGradient></defs>"
    '<rect width="100" height="100" rx="22" fill="url(#g)"/>'
    '<text x="50" y="71" font-family="system-ui,-apple-system,sans-serif" '
    'font-size="62" font-weight="700" fill="#ffffff" text-anchor="middle">S</text>'
    "</svg>"
)


def favicon(request: HttpRequest) -> HttpResponse:
    """Serve the SVG favicon (for the backend URL's browser tab and the admin)."""
    return HttpResponse(FAVICON_SVG, content_type="image/svg+xml")


@extend_schema(
    responses=ServiceDescriptorSerializer,
    summary="Service descriptor",
    description="A small, human-readable JSON descriptor listing the service's endpoints.",
)
@api_view(["GET"])
def index(request: Request) -> Response:
    """Root endpoint — a small, human-readable service descriptor."""
    return Response(
        {
            "service": "portfolio-backend",
            "status": "ok",
            "endpoints": {
                "health": "/health",
                "docs": "/api/docs/",
                "schema": "/api/schema/",
                "chat_stream": "/chat/stream",
                "analytics_proxy": "/ingest/<path>",
            },
        }
    )


@extend_schema(
    responses=HealthSerializer,
    summary="Liveness probe",
    description="Liveness probe used by the host's health check (Render) and uptime monitor.",
)
# Answer HEAD as well as GET: uptime monitors (UptimeRobot) probe with HEAD by default,
# and a GET-only DRF view rejects HEAD with 405 — which the monitor reads as the service
# being down even though it's healthy. Render's own check uses GET, so it was unaffected.
@api_view(["GET", "HEAD"])
def health(request: Request) -> Response:
    """Liveness probe used by the host's health check."""
    return Response({"status": "ok"})
