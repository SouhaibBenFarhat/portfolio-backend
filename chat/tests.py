"""Tests for the chat app.

Phase 0 guards: app wiring, async stack, DB config.
Phase 1 guards: the streaming endpoint (LiteLLM mocked, so no API key is needed).
Phase 2 guards: conversation persistence and memory across messages.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
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


# --- helpers ---------------------------------------------------------------


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


async def _drain(response) -> str:
    parts = [chunk async for chunk in response.streaming_content]
    return b"".join(parts).decode()


def _conversation_id_from(body: str) -> str:
    for frame in body.split("\n\n"):
        line = frame.replace("data: ", "").strip()
        if not line:
            continue
        data = json.loads(line)
        if "conversation_id" in data:
            return data["conversation_id"]
    raise AssertionError("no conversation_id frame in stream")


# --- Phase 1: streaming ----------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_chat_stream_streams_tokens_as_sse():
    """POST a message → get a conversation id, token frames, then a done frame."""

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
            return await _drain(response)

    body = asyncio.run(_run())
    assert '"conversation_id"' in body
    assert '"text": "Hel"' in body
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


# --- Phase 2: persistence & memory ----------------------------------------


@pytest.mark.django_db(transaction=True)
def test_messages_are_persisted():
    async def _run():
        from chat.models import Message

        fake = AsyncMock(return_value=_FakeStream(["Hello"]))
        with patch("chat.views.litellm.acompletion", fake):
            response = await AsyncClient().post(
                "/chat/stream",
                data=json.dumps({"message": "hi"}),
                content_type="application/json",
            )
            await _drain(response)
        return sorted([m.role async for m in Message.objects.all()])

    assert asyncio.run(_run()) == ["assistant", "user"]


@pytest.mark.django_db(transaction=True)
def test_conversation_is_remembered_across_messages():
    """A second message on the same conversation includes the first exchange."""

    async def _run():
        fake = AsyncMock(side_effect=[_FakeStream(["Hi ", "Sam"]), _FakeStream(["you said hi"])])
        with patch("chat.views.litellm.acompletion", fake):
            client = AsyncClient()
            first = await client.post(
                "/chat/stream",
                data=json.dumps({"message": "I am Sam"}),
                content_type="application/json",
            )
            conversation_id = _conversation_id_from(await _drain(first))

            second = await client.post(
                "/chat/stream",
                data=json.dumps({"message": "what did I say?", "conversation_id": conversation_id}),
                content_type="application/json",
            )
            await _drain(second)
        return fake.call_args_list

    calls = asyncio.run(_run())
    second_messages = calls[1].kwargs["messages"]
    contents = [m["content"] for m in second_messages]
    assert contents == ["I am Sam", "Hi Sam", "what did I say?"]
