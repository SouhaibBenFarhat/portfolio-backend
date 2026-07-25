"""Basic service endpoints.

`/` and `/health` are DRF views so drf-spectacular documents them in the OpenAPI
schema. `favicon` stays a plain Django view — it serves an image, not JSON API.
"""

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.templatetags.static import static
from drf_spectacular.utils import extend_schema
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from .serializers import HealthSerializer, ServiceDescriptorSerializer

# The hirees.me mark: a CV with a brain badged into its corner — a document that thinks.
# Composed from two Lucide icons (file-text + brain, ISC licensed, https://lucide.dev),
# the same icon set the frontend uses, so the mark and the interface stay consistent.
#
# The disc behind the brain is filled with the tile colour rather than left transparent:
# it knocks the page's own strokes out from behind the badge, so the two icons read as one
# object instead of overlapping line work.
#
# Petrol (#1f6f78) is the site's accent — see the token block in the landing template. The
# previous favicon was an "S" on an indigo→violet gradient, which belonged to neither the
# backend's palette nor hirees.me's.
_MARK_COLOUR = "#1f6f78"

# The page, its folded corner, and the brain — the parts every size keeps.
_PAGE = (
    '<path d="M6 22a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h8a2.4 2.4 0 0 1 1.704.706l3.588 3.588'
    'A2.4 2.4 0 0 1 20 8v12a2 2 0 0 1-2 2z"/>'
    '<path d="M14 2v5a1 1 0 0 0 1 1h5"/>'
)
_LINES = '<path d="M10 9H8"/><path d="M16 13H8"/><path d="M16 17H8"/>'
_BRAIN = (
    '<path d="M12 18V5"/>'
    '<path d="M15 13a4.17 4.17 0 0 1-3-4 4.17 4.17 0 0 1-3 4"/>'
    '<path d="M17.598 6.5A3 3 0 1 0 12 5a3 3 0 1 0-5.598 1.5"/>'
    '<path d="M17.997 5.125a4 4 0 0 1 2.526 5.77"/>'
    '<path d="M18 18a4 4 0 0 0 2-7.464"/>'
    '<path d="M19.967 17.483A4 4 0 1 1 12 18a4 4 0 1 1-7.967-.517"/>'
    '<path d="M6 18a4 4 0 0 1-2-7.464"/>'
    '<path d="M6.003 5.125a4 4 0 0 0-2.526 5.77"/>'
)


def _mark(*, lines: bool, stroke: float, badge: float) -> str:
    """The mark on a petrol tile. `badge` scales the brain; `lines` keeps the page's text.

    The disc behind the brain is filled with the tile colour rather than left transparent,
    so the page's own strokes stop behind the badge and the two icons read as one object.
    """
    offset = 18 - 12 * badge  # keeps the brain centred on the disc at any scale
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
        f'<rect width="32" height="32" rx="7" fill="{_MARK_COLOUR}"/>'
        f'<g transform="translate(4 4)" fill="none" stroke="#ffffff" stroke-width="{stroke}" '
        'stroke-linecap="round" stroke-linejoin="round">'
        + _PAGE
        + (_LINES if lines else "")
        + f'<circle cx="18" cy="18" r="6.6" fill="{_MARK_COLOUR}" stroke="none"/>'
        + f'<g transform="translate({offset} {offset}) scale({badge})">{_BRAIN}</g>'
        + "</g></svg>"
    )


# Full mark, for anywhere with room: 32px and up.
FAVICON_SVG = _mark(lines=True, stroke=2, badge=0.46)

# Small variant, for the browser tab. A page, a brain and three lines of text is too much
# detail for 16 pixels — the lines merge into the brain and the whole thing turns to mud.
# So the text goes, the brain grows, and the strokes thicken. Redrawing a mark for small
# sizes is normal practice; scaling one down is what produces the mud.
FAVICON_SMALL_SVG = _mark(lines=False, stroke=2.4, badge=0.54)


def favicon(request: HttpRequest) -> HttpResponse:
    """Serve the SVG favicon (for the browser tab and the admin).

    Serves the small variant: this route is what a tab actually loads, and a tab renders at
    16–20px. The full mark is available at /favicon-full.svg for anywhere with more room.
    """
    return HttpResponse(FAVICON_SMALL_SVG, content_type="image/svg+xml")


def favicon_full(request: HttpRequest) -> HttpResponse:
    """The full mark, text lines included — for app icons, avatars and social cards."""
    return HttpResponse(FAVICON_SVG, content_type="image/svg+xml")


def landing(request: HttpRequest) -> HttpResponse:
    """The hirees.me landing page.

    A plain Django view: the template's styles and script are inline, so the page itself
    renders on a cold instance with nothing collected. This took the root path from the
    JSON service descriptor, which moved to /api/ — a marketing page belongs at the domain
    root, and machines can read the descriptor from a path that says it is for machines.

    The link-preview URLs are built here rather than hardcoded: Open Graph requires
    absolute URLs, and this service answers on both hirees.me and onrender.com, so
    deriving them from the request keeps the card correct on either host.
    """
    return render(
        request,
        "core/landing.html",
        {
            "og_url": request.build_absolute_uri("/"),
            "og_image": request.build_absolute_uri(static("core/og.png")),
        },
    )


@extend_schema(
    responses=ServiceDescriptorSerializer,
    summary="Service descriptor",
    description="A small, human-readable JSON descriptor listing the service's endpoints.",
)
@api_view(["GET"])
def index(request: Request) -> Response:
    """Service descriptor, at /api/ — the root path serves the landing page."""
    return Response(
        {
            "service": "portfolio-backend",
            "status": "ok",
            "endpoints": {
                "landing": "/",
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
