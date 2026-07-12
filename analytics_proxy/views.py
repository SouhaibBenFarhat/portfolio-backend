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

# Request headers that must not be forwarded verbatim.
_DROP_REQUEST_HEADERS = {"host", "content-length", "connection"}
# Response headers we drop: hop-by-hop, length (recomputed by Django), and CORS headers
# (our own CorsMiddleware sets the correct ones). NOTE: Content-Encoding is deliberately
# NOT dropped — we pass the upstream's compressed body through untouched so the browser
# decodes it. PostHog's CDN serves the recorder as brotli/zstd, which Python `requests`
# cannot decode; stripping the header while forwarding compressed bytes = corrupt JS.
_DROP_RESPONSE_HEADERS = {
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
            stream=True,
        )
        # Read the RAW (still-encoded) body without letting requests auto-decode it, so
        # the body and its Content-Encoding header stay consistent when passed to the
        # browser. requests only decodes gzip/deflate — PostHog's brotli/zstd recorder
        # would otherwise reach the browser as corrupt "plain" JS.
        content = upstream.raw.read(decode_content=False)
    except requests.RequestException:
        return HttpResponse("Analytics upstream unavailable", status=502)

    response = HttpResponse(
        content,
        status=upstream.status_code,
        content_type=upstream.headers.get("Content-Type", "application/octet-stream"),
    )
    for key, value in upstream.headers.items():
        lower = key.lower()
        if lower == "content-type" or lower in _DROP_RESPONSE_HEADERS:
            continue
        if lower.startswith("access-control-"):
            continue  # let CorsMiddleware own CORS
        response[key] = value  # includes Content-Encoding so the browser can decode
    return response
