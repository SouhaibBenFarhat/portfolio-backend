"""
First-party reverse proxy for PostHog.

The browser talks to `/ingest/*` on *our* domain instead of `*.i.posthog.com`, so
ad-blockers and tracker blocklists (which target `posthog.com`) don't drop the
requests. We forward each call to the correct PostHog EU host and stream the response
back untouched.

Routing (mirrors PostHog's recommended proxy layout):
  /ingest/static/*  ->  https://eu-assets.i.posthog.com/static/*   (library assets)
  /ingest/*         ->  https://eu.i.posthog.com/*                 (events, decide, ...)
"""

import requests
from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt

ASSET_HOST = "https://eu-assets.i.posthog.com"
INGEST_HOST = "https://eu.i.posthog.com"

# Request headers that must not be forwarded verbatim. We drop Accept-Encoding so the
# upstream negotiates plain gzip/deflate with `requests` (which it decodes) instead of
# the browser's brotli/zstd. PostHog's CDN serves brotli by default; `requests` can't
# decode it, and forwarding brotli to the browser breaks strict decoders — Safari renders
# the recorder script as corrupt bytes (`�`) and Session Replay never starts.
_DROP_REQUEST_HEADERS = {"host", "content-length", "connection", "accept-encoding"}
# Response headers we drop: hop-by-hop, and length/encoding (Django recomputes length;
# `requests` already decoded the body, so we emit it as plain, un-encoded bytes). CORS
# headers are dropped too so our own CorsMiddleware owns them.
_DROP_RESPONSE_HEADERS = {
    "content-encoding",
    "content-length",
    "transfer-encoding",
    "connection",
    "keep-alive",
}

_UPSTREAM_TIMEOUT = 30  # seconds


def _client_ip(request: HttpRequest) -> str:
    """Real visitor IP so PostHog geolocation isn't attributed to the proxy."""
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


@csrf_exempt
def posthog_proxy(request: HttpRequest, subpath: str) -> HttpResponse:
    base = ASSET_HOST if subpath.startswith("static/") else INGEST_HOST
    url = f"{base}/{subpath}"
    query = request.META.get("QUERY_STRING", "")
    if query:
        url = f"{url}?{query}"

    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in _DROP_REQUEST_HEADERS
    }
    client_ip = _client_ip(request)
    if client_ip:
        headers["X-Forwarded-For"] = client_ip

    body = request.body if request.method in {"POST", "PUT", "PATCH"} else None

    try:
        upstream = requests.request(
            method=request.method,
            url=url,
            data=body,
            headers=headers,
            timeout=_UPSTREAM_TIMEOUT,
            allow_redirects=False,
        )
    except requests.RequestException:
        return HttpResponse("Analytics upstream unavailable", status=502)

    # `requests` transparently decodes the gzip/deflate it negotiated, so `.content` is
    # the plain body. We return it un-encoded (Content-Encoding stripped), which every
    # browser — including Safari — reads correctly.
    response = HttpResponse(
        upstream.content,
        status=upstream.status_code,
        content_type=upstream.headers.get("Content-Type", "application/octet-stream"),
    )
    for key, value in upstream.headers.items():
        lower = key.lower()
        if lower == "content-type" or lower in _DROP_RESPONSE_HEADERS:
            continue
        if lower.startswith("access-control-"):
            continue  # let CorsMiddleware own CORS
        response[key] = value
    return response
