from unittest.mock import MagicMock, patch

from django.test import Client


def _fake_upstream(*, status=200, content=b"ok", headers=None):
    response = MagicMock()
    response.status_code = status
    response.content = content  # requests has already decoded gzip/deflate here
    response.headers = headers or {"Content-Type": "text/plain"}
    return response


@patch("analytics_proxy.views.requests.request")
def test_static_path_routes_to_asset_host(mock_request):
    mock_request.return_value = _fake_upstream(
        content=b"console.log(1)", headers={"Content-Type": "application/javascript"}
    )
    response = Client().get("/ingest/static/array.js")

    assert response.status_code == 200
    assert response["Content-Type"] == "application/javascript"
    called_url = mock_request.call_args.kwargs["url"]
    assert called_url == "https://eu-assets.i.posthog.com/static/array.js"


@patch("analytics_proxy.views.requests.request")
def test_event_path_routes_to_ingest_host_with_query(mock_request):
    mock_request.return_value = _fake_upstream(content=b"1")
    response = Client().get("/ingest/e/?ver=1.2.3")

    assert response.status_code == 200
    called_url = mock_request.call_args.kwargs["url"]
    assert called_url == "https://eu.i.posthog.com/e/?ver=1.2.3"


@patch("analytics_proxy.views.requests.request")
def test_post_body_is_forwarded(mock_request):
    mock_request.return_value = _fake_upstream(content=b"1")
    Client().post("/ingest/e/", data=b"payload", content_type="application/json")

    assert mock_request.call_args.kwargs["data"] == b"payload"


@patch("analytics_proxy.views.requests.request")
def test_browser_accept_encoding_not_forwarded(mock_request):
    # The browser's Accept-Encoding (brotli/zstd) must NOT be forwarded — requests can't
    # decode those and Safari can't read them through the proxy. requests negotiates gzip.
    mock_request.return_value = _fake_upstream(content=b"1")
    Client().get(
        "/ingest/static/recorder.js",
        HTTP_ACCEPT_ENCODING="gzip, deflate, br, zstd",
    )
    forwarded = mock_request.call_args.kwargs["headers"]
    assert not any(k.lower() == "accept-encoding" for k in forwarded)


@patch("analytics_proxy.views.requests.request")
def test_encoding_and_cors_headers_stripped(mock_request):
    # requests already decoded the body, so we emit plain bytes: Content-Encoding is
    # stripped (else the browser would try to decode plain text). CORS is stripped too.
    mock_request.return_value = _fake_upstream(
        headers={
            "Content-Type": "application/javascript",
            "Content-Encoding": "gzip",
            "Access-Control-Allow-Origin": "https://evil.example",
        }
    )
    response = Client().get("/ingest/static/recorder.js")

    assert "Content-Encoding" not in response
    assert response.get("Access-Control-Allow-Origin") != "https://evil.example"


@patch("analytics_proxy.views.requests.request")
def test_upstream_failure_returns_502(mock_request):
    import requests

    mock_request.side_effect = requests.RequestException("boom")
    response = Client().get("/ingest/e/")

    assert response.status_code == 502


@patch("analytics_proxy.views.requests.request")
def test_large_recording_snapshot_is_not_rejected(mock_request):
    # Session-recording snapshots can exceed Django's 2.5MB default body limit.
    # DATA_UPLOAD_MAX_MEMORY_SIZE must be raised, or Django 400s before the proxy runs.
    mock_request.return_value = _fake_upstream(content=b"1")
    big_body = b"x" * (5 * 1024 * 1024)  # 5 MB
    response = Client().post("/ingest/s/", data=big_body, content_type="text/plain")

    assert response.status_code == 200
    assert mock_request.call_args.kwargs["data"] == big_body
