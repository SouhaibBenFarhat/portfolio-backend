from django.test import Client


def test_health_ok():
    response = Client().get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_index_describes_service():
    response = Client().get("/")
    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "portfolio-backend"
    assert "/ingest/<path>" in body["endpoints"]["analytics_proxy"]
