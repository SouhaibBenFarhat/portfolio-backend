"""Tests for the chat app.

Phase 0 guards: app wiring, async stack, DB config.
Phase 1/3a guards: streaming through the LangGraph agent (a fake model, so no
API key is needed).
Phase 2 guards: conversation persistence and memory across messages.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.conf import settings
from django.test import AsyncClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from chat.agent import build_agent

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


def _fake_model(reply: str):
    """A LangGraph-compatible chat model that streams a scripted reply."""
    return GenericFakeChatModel(messages=iter([AIMessage(content=reply)]))


class _RecordingAgent:
    """Stands in for the compiled agent: records its inputs, streams a reply."""

    def __init__(self, reply: str = "ok"):
        self.seen = []
        self._reply = reply

    async def astream(self, payload, stream_mode=None):
        self.seen.append(payload["messages"])
        yield SimpleNamespace(content=self._reply), {}


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


# --- Phase 1 / 3a: streaming through the LangGraph agent -------------------


@pytest.mark.django_db(transaction=True)
def test_chat_stream_streams_tokens_via_langgraph():
    """A real LangGraph agent (with a fake model) streams SSE token frames."""

    async def _run():
        agent = build_agent(model=_fake_model("Hello recruiter"))
        with patch("chat.views.get_agent", return_value=agent):
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
    assert '"text":' in body
    assert "Hello" in body
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

        with patch("chat.views.get_agent", return_value=_RecordingAgent()):
            response = await AsyncClient().post(
                "/chat/stream",
                data=json.dumps({"message": "hi"}),
                content_type="application/json",
            )
            await _drain(response)
        return sorted([m.role async for m in Message.objects.all()])

    assert asyncio.run(_run()) == ["assistant", "user"]


@pytest.mark.django_db(transaction=True)
def test_conversation_history_is_passed_to_the_agent():
    """A second message on the same conversation includes the first exchange."""
    recording = _RecordingAgent(reply="noted")

    async def _run():
        with patch("chat.views.get_agent", return_value=recording):
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
        return recording.seen

    seen = asyncio.run(_run())
    contents = [m["content"] for m in seen[1]]
    assert contents == ["I am Sam", "noted", "what did I say?"]
