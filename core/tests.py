import yaml
from django.test import Client


def test_health_ok():
    response = Client().get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_accepts_head_requests():
    """Uptime monitors (UptimeRobot) default to HEAD; a GET-only view answers 405,
    which reads as the service being down even though it's healthy."""
    response = Client().head("/health")
    assert response.status_code == 200


def test_index_describes_service():
    """The descriptor moved to /api/ when the landing page took the root path."""
    response = Client().get("/api/")
    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "portfolio-backend"
    assert "/ingest/<path>" in body["endpoints"]["analytics_proxy"]


def test_root_serves_the_landing_page():
    response = Client().get("/")
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/html")
    body = response.content.decode()
    assert "hirees" in body
    # The page must stand alone: no {% static %} dependency, so it renders on a cold
    # instance with nothing collected.
    assert "{% static" not in body


def test_favicon_uses_the_small_mark_and_the_full_one_is_available():
    """The tab renders at 16px, so /favicon.svg serves the variant without text lines."""
    small = Client().get("/favicon.svg").content.decode()
    full = Client().get("/favicon-full.svg").content.decode()
    assert small.count("<path") < full.count("<path")
    assert "linearGradient" not in small  # the old gradient monogram is gone
    assert "#1f6f78" in small  # the site's petrol accent


def test_favicon_is_served_as_svg():
    response = Client().get("/favicon.ico")
    assert response.status_code == 200
    assert response["Content-Type"] == "image/svg+xml"
    assert b"<svg" in response.content


# --- OpenAPI schema + Swagger UI ------------------------------------------


def test_openapi_schema_is_served():
    response = Client().get("/api/schema/")
    assert response.status_code == 200
    body = response.content.decode()
    assert body.startswith("openapi:")
    assert "portfolio-backend API" in body


def test_swagger_ui_is_served():
    response = Client().get("/api/docs/")
    assert response.status_code == 200
    assert b"swagger-ui" in response.content.lower()


def test_schema_documents_all_public_endpoints():
    """The schema covers the JSON endpoints (DRF) and the injected streaming endpoint."""
    spec = yaml.safe_load(Client().get("/api/schema/").content)
    paths = spec["paths"]
    assert "/health" in paths
    assert "/chat/conversations/{conversation_id}/" in paths
    assert (
        "/chat/conversations/{conversation_id}/messages/{message_id}/rating/" in paths
    )  # the DRF rating endpoint
    assert "/chat/stream" in paths  # injected by the postprocessing hook
    assert "/ingest/{subpath}" not in paths  # proxy stays out of the docs

    schemas = spec["components"]["schemas"]
    assert "ChatStreamRequest" in schemas
    assert "ChatStreamFrame" in schemas  # the SSE-frame union the frontend types from
    assert "ChatSuggestionsFrame" in schemas  # the follow-up chips joined the union
    assert "ChatMessageIdFrame" in schemas  # the rateable-reply id joined the union
