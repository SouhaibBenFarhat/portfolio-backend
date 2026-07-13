"""Basic service endpoints."""

from django.http import HttpRequest, HttpResponse, JsonResponse

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


def index(request: HttpRequest) -> JsonResponse:
    """Root endpoint — a small, human-readable service descriptor."""
    return JsonResponse(
        {
            "service": "portfolio-backend",
            "status": "ok",
            "endpoints": {
                "health": "/health",
                "analytics_proxy": "/ingest/<path>",
            },
        }
    )


def health(request: HttpRequest) -> JsonResponse:
    """Liveness probe used by the host's health check."""
    return JsonResponse({"status": "ok"})
