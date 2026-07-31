"""Basic service endpoints.

`/` and `/health` are DRF views so drf-spectacular documents them in the OpenAPI
schema. `favicon` stays a plain Django view — it serves an image, not JSON API.
"""

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.templatetags.static import static
from django.urls import reverse
from drf_spectacular.utils import extend_schema
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response

from .providers import PROVIDERS, configured_provider_ids
from .serializers import HealthSerializer, MeSerializer, ServiceDescriptorSerializer

# The hirees.me mark: a lowercase "h" closed by the full stop of ".me", on a petrol tile.
# Drawn on Lucide's 24x24 grid with its stroke conventions (round caps and joins, no fill,
# https://lucide.dev) — the same icon set the frontend uses — so the mark sits in the same
# drawing language as the interface around it, even though it is a letter and not an icon.
#
# The shoulder turns through a 2.1 corner rather than a right angle. A hard corner reads as
# interface chrome and argues with the Fraunces wordmark beside it; a fully round arch reads
# as a typeface the site doesn't own and has no business imitating. The radius is the
# settlement, and it tracks the stroke weight.
#
# It replaced a CV with a brain badged into its corner. That mark needed two separate
# drawings, because at 16px its three text lines merged into the badge and the whole thing
# turned to mud — a problem a letterform simply does not have. That is the argument for a
# monogram here, more than any aesthetic one.
#
# Petrol (#1f6f78) is the site's accent — see the token block in the landing template.
_MARK_COLOUR = "#1f6f78"

# The letter, on Lucide's 24x24 grid: an ascender, then a shoulder that turns down into the
# leg. Ink is centred on the box in both axes — the dot's mass out to the right is what
# balances the stem's out to the left, so the two are drawn as one composition, not a glyph
# with punctuation added afterwards.
#
# Two numbers here were set by looking at the thing rendered, not by arithmetic, and both
# only misbehave at tab size:
#   - the x-height is generous (8.8 of a 17.2 ascender). A tall x-height is what keeps a
#     lowercase letter legible when it is small, because the counter is the first thing to
#     silt up; an elegant small counter reads as a smudge at 16px.
#   - the shoulder is narrow enough to leave real air before the dot. Set tight, the dot
#     welds onto the leg and stops reading as punctuation — it becomes a bullet.
_BASELINE = 20.6
_DOT_X = 18.0
_LETTER = '<path d="M5.6 3.4V20.6"/><path d="M5.6 11.8h6a2 2 0 0 1 2 2V20.6"/>'


def _mark(*, stroke: float, dot: float) -> str:
    """The mark on a petrol tile, at one optical size.

    One drawing serves every size — that is the whole point of a letter here. What changes
    is weight: small sizes need more of it, the way a type family cuts a heavier face for
    small text rather than shrinking the display one. Thin strokes and a small dot are the
    first things a 16px raster throws away.

    The dot sits *on* the baseline, so its centre has to rise as its radius grows.
    """
    full_stop = (
        f'<circle cx="{_DOT_X}" cy="{round(_BASELINE - dot, 3)}" r="{dot}" '
        'fill="#ffffff" stroke="none"/>'
    )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
        f'<rect width="32" height="32" rx="7" fill="{_MARK_COLOUR}"/>'
        f'<g transform="translate(4 4)" fill="none" stroke="#ffffff" stroke-width="{stroke}" '
        'stroke-linecap="round" stroke-linejoin="round">' + _LETTER + full_stop + "</g></svg>"
    )


# Full mark, for anywhere with room: 32px and up.
FAVICON_SVG = _mark(stroke=2.3, dot=1.5)

# The browser tab, which renders at 16-20px. Same letter, more weight.
FAVICON_SMALL_SVG = _mark(stroke=2.7, dot=1.75)


def favicon(request: HttpRequest) -> HttpResponse:
    """Serve the SVG favicon (for the browser tab and the admin).

    Serves the small variant: this route is what a tab actually loads, and a tab renders at
    16–20px. The full mark is available at /favicon-full.svg for anywhere with more room.
    """
    return HttpResponse(FAVICON_SMALL_SVG, content_type="image/svg+xml")


def favicon_full(request: HttpRequest) -> HttpResponse:
    """The mark at its display weight — for app icons, avatars and social cards."""
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
            # One-click sign-in in the hero: Google is the fast path (most visitors have an
            # account, one tap), with a "more options" link to the full /signin page for the
            # rest. reverse() so the route stays correct if it ever moves.
            "google_login_url": reverse("google_login"),
            # When Google has no credential, the hero button routes to the friendly notice
            # instead of erroring (same graceful handling as /signin).
            "google_configured": "google" in configured_provider_ids(request),
        },
    )


def signin(request: HttpRequest) -> HttpResponse:
    """The sign-in / sign-up page.

    Social login unifies the two: the first time someone continues with a provider their
    account is created; after that the same button signs them in. Server-rendered in the
    landing page's design so the whole entry flow reads as one product — the authenticated
    dashboard is a separate SPA (see plans/auth-multitenancy-plan.md).

    All three providers are shown; a configured one's button posts to the allauth URL that
    starts its OAuth flow, while an unconfigured one's button routes back here with
    ?unavailable= so a click shows a friendly "not set up yet" notice instead of dead-ending.
    An already-authenticated visitor is sent straight on.
    """
    if request.user.is_authenticated:
        return redirect(settings.LOGIN_REDIRECT_URL)
    # The provider buttons come from the {% social_buttons %} tag (shared with login/signup).
    # A provider the visitor tried that isn't set up is validated against the known names, so
    # the notice can never render arbitrary injected text.
    requested = request.GET.get("unavailable", "")
    unavailable = requested if requested in {p["name"] for p in PROVIDERS} else ""
    return render(request, "core/signin.html", {"unavailable": unavailable})


@extend_schema(
    responses=MeSerializer,
    summary="Current session",
    description="Whether this browser holds a signed-in backend session, and who it belongs to. "
    "The dashboard SPA (app.hirees.me) has no sign-in UI of its own — it calls this endpoint "
    "with credentials to gate itself, and sends the visitor to the backend's /signin when the "
    "response is unauthenticated. Always 200; read the `authenticated` flag.",
)
@api_view(["GET"])
@authentication_classes([SessionAuthentication])  # read the session cookie, for this view only
@permission_classes([AllowAny])
def me(request: Request) -> Response:
    """Report the caller's backend session, for the dashboard SPA's auth gate."""
    user = request.user
    if not user.is_authenticated:
        return Response({"authenticated": False, "email": "", "display": ""})
    return Response(
        {
            "authenticated": True,
            "email": user.email or "",
            "display": user.get_full_name() or user.get_username(),
        }
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
                "me": "/api/me",
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
