"""Basic service endpoints."""

from django.http import HttpRequest, JsonResponse


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
