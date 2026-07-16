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
    response = Client().get("/")
    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "portfolio-backend"
    assert "/ingest/<path>" in body["endpoints"]["analytics_proxy"]


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
    assert "/chat/stream" in paths  # injected by the postprocessing hook
    assert "/ingest/{subpath}" not in paths  # proxy stays out of the docs

    schemas = spec["components"]["schemas"]
    assert "ChatStreamRequest" in schemas
    assert "ChatStreamFrame" in schemas  # the SSE-frame union the frontend types from
