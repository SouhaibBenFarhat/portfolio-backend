"""Tests for the chat app.

Phase 0 guards: app wiring, async stack, DB config.
Phase 1 guards: the streaming endpoint (LiteLLM mocked, so no API key is needed).
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from django.conf import settings
from django.test import AsyncClient

# --- Phase 0: infrastructure ----------------------------------------------


def test_chat_app_is_installed():
    assert "chat" in settings.INSTALLED_APPS


def test_asgi_application_is_importable():
    """The async entry point must load — it's what the production server runs."""
    from config.asgi import application

    assert callable(application)


def test_database_is_configured_for_sqlite_or_postgres():
    engine = settings.DATABASES["default"]["ENGINE"]
    assert engine in {
        "django.db.backends.sqlite3",
        "django.db.backends.postgresql",
    }


def test_health_endpoint_works_through_the_async_stack():
    """Existing sync endpoints must still serve under the new ASGI server."""

    async def _get():
        return await AsyncClient().get("/health")

    response = asyncio.run(_get())
    assert response.status_code == 200


# --- Phase 1: streaming chat ----------------------------------------------


def _chunk(token: str):
    """Mimic a LiteLLM streaming chunk carrying one token."""
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=token))])


class _FakeStream:
    """Async-iterable stand-in for LiteLLM's streaming response."""

    def __init__(self, tokens):
        self._tokens = tokens

    def __aiter__(self):
        return self._aiter()

    async def _aiter(self):
        for token in self._tokens:
            yield _chunk(token)


def test_demo_page_renders():
    async def _get():
        return await AsyncClient().get("/chat/")

    response = asyncio.run(_get())
    assert response.status_code == 200
    assert b"Chat streaming demo" in response.content


def test_chat_stream_streams_tokens_as_sse():
    """POST a message → get token frames then a done frame, all Server-Sent Events."""

    async def _run():
        fake = AsyncMock(return_value=_FakeStream(["Hel", "lo", "!"]))
        with patch("chat.views.litellm.acompletion", fake):
            response = await AsyncClient().post(
                "/chat/stream",
                data=json.dumps({"message": "hi"}),
                content_type="application/json",
            )
            assert response.status_code == 200
            assert response["Content-Type"] == "text/event-stream"
            parts = [chunk async for chunk in response.streaming_content]
        return b"".join(parts).decode()

    body = asyncio.run(_run())
    assert '"text": "Hel"' in body
    assert '"text": "lo"' in body
    assert '"done": true' in body


def test_chat_stream_rejects_non_post():
    async def _run():
        return await AsyncClient().get("/chat/stream")

    assert asyncio.run(_run()).status_code == 405


def test_chat_stream_requires_a_message():
    async def _run():
        return await AsyncClient().post(
            "/chat/stream", data=json.dumps({}), content_type="application/json"
        )

    assert asyncio.run(_run()).status_code == 400
