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
from django.contrib import admin as django_admin
from django.test import AsyncClient, override_settings
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


def _model_stream_event(text: str) -> dict:
    return {
        "event": "on_chat_model_stream",
        "name": "model",
        "data": {"chunk": SimpleNamespace(content=text)},
    }


class _RecordingAgent:
    """Stands in for the compiled agent: records its inputs, streams a reply."""

    def __init__(self, reply: str = "ok"):
        self.seen = []
        self._reply = reply

    async def astream_events(self, payload, version=None):
        self.seen.append(payload["messages"])
        yield _model_stream_event(self._reply)


class _ToolEventAgent:
    """Stand-in that emits a tool-start/tool-end pair then a token."""

    async def astream_events(self, payload, version=None):
        yield {"event": "on_tool_start", "name": "get_facts", "data": {}}
        yield {"event": "on_tool_end", "name": "get_facts", "data": {}}
        yield _model_stream_event("Here you go")


class _FailingAgent:
    """Stand-in whose stream raises immediately (simulates a quota/rate limit)."""

    async def astream_events(self, payload, version=None):
        raise RuntimeError("rate limit exceeded")
        yield  # unreachable; makes this an async generator


class _FinalOnlyAgent:
    """Stand-in that emits a tool then a final message, but streams no tokens."""

    async def astream_events(self, payload, version=None):
        yield {"event": "on_tool_start", "name": "get_facts", "data": {}}
        yield {"event": "on_tool_end", "name": "get_facts", "data": {}}
        yield {
            "event": "on_chat_model_end",
            "name": "model",
            "data": {"output": SimpleNamespace(content="Here is the answer.")},
        }


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
        agent = build_agent(model=_fake_model("Hello recruiter"), tools=[])
        with patch("chat.views.build_agents", return_value=(agent,)):
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


@pytest.mark.django_db(transaction=True)
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

        with patch("chat.views.build_agents", return_value=(_RecordingAgent(),)):
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
        with patch("chat.views.build_agents", return_value=(recording,)):
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


# --- Phase 3b: knowledge base + admin -------------------------------------


def test_fact_and_document_registered_in_admin():
    from chat.models import Conversation, Document, Fact

    assert Fact in django_admin.site._registry
    assert Document in django_admin.site._registry
    assert Conversation in django_admin.site._registry


@pytest.mark.django_db
def test_admin_login_page_loads(client):
    """Admin is enabled and reachable (its middleware/apps are wired correctly)."""
    response = client.get("/admin/login/")
    assert response.status_code == 200


@pytest.mark.django_db
def test_fact_and_document_models_work():
    from chat.models import Document, Fact

    fact = Fact.objects.create(
        category="Compensation", question="Salary expectations", answer="Competitive"
    )
    assert str(fact) == "Compensation: Salary expectations"
    assert fact.is_active is True

    doc = Document.objects.create(slug="cv", title="Résumé", content="…")
    assert str(doc) == "Résumé"


# --- Phase 3c: tools + step events ----------------------------------------


@pytest.mark.django_db(transaction=True)
def test_stream_emits_tool_step_events():
    """Tool start/end events from the agent become SSE `tool` frames."""

    async def _run():
        with patch("chat.views.build_agents", return_value=(_ToolEventAgent(),)):
            response = await AsyncClient().post(
                "/chat/stream",
                data=json.dumps({"message": "what are your projects?"}),
                content_type="application/json",
            )
            return await _drain(response)

    body = asyncio.run(_run())
    assert '"tool": "get_facts"' in body
    assert '"status": "start"' in body
    assert '"status": "end"' in body
    assert '"text": "Here you go"' in body
    assert '"done": true' in body


@pytest.mark.django_db(transaction=True)
def test_get_facts_tool_reads_active_facts():
    from chat.models import Fact
    from chat.tools import get_facts

    async def _run():
        await Fact.objects.acreate(category="Personal", question="Hobbies", answer="Chess")
        await Fact.objects.acreate(
            category="Personal", question="Secret", answer="hidden", is_active=False
        )
        return await get_facts.ainvoke({"category": ""})

    result = asyncio.run(_run())
    assert "Hobbies: Chess" in result
    assert "hidden" not in result  # inactive facts are excluded


@pytest.mark.django_db(transaction=True)
def test_get_cv_tool_reads_the_cv_document():
    from chat.models import Document
    from chat.tools import get_cv

    async def _run():
        await Document.objects.acreate(slug="cv", title="CV", content="10 years of Python")
        return await get_cv.ainvoke({})

    assert "10 years of Python" in asyncio.run(_run())


def test_list_github_projects_tool_formats_repos():
    from chat import tools

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return [
                {
                    "name": "portfolio-backend",
                    "language": "Python",
                    "stargazers_count": 3,
                    "description": "Django backend",
                    "fork": False,
                },
                {"name": "a-fork", "fork": True, "stargazers_count": 0},
            ]

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            return _Resp()

    async def _run():
        with patch.object(tools.httpx, "AsyncClient", return_value=_Client()):
            return await tools.list_github_projects.ainvoke({})

    result = asyncio.run(_run())
    assert "portfolio-backend" in result
    assert "Django backend" in result
    assert "a-fork" not in result  # forks are excluded


# --- Phase 4: provider failover -------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_failover_to_second_model_when_first_fails():
    """First model errors before streaming → the next model handles the turn."""
    fallback = _RecordingAgent(reply="from the fallback")

    async def _run():
        with patch("chat.views.build_agents", return_value=(_FailingAgent(), fallback)):
            response = await AsyncClient().post(
                "/chat/stream",
                data=json.dumps({"message": "hi"}),
                content_type="application/json",
            )
            return await _drain(response)

    body = asyncio.run(_run())
    assert '"error"' not in body  # the failure was recovered, not shown
    assert '"text": "from the fallback"' in body
    assert '"done": true' in body


@pytest.mark.django_db(transaction=True)
def test_final_message_is_sent_when_the_model_does_not_stream_tokens():
    """If the answer arrives only as a final message, it's still sent as text."""

    async def _run():
        with patch("chat.views.build_agents", return_value=(_FinalOnlyAgent(),)):
            response = await AsyncClient().post(
                "/chat/stream",
                data=json.dumps({"message": "hi"}),
                content_type="application/json",
            )
            return await _drain(response)

    body = asyncio.run(_run())
    assert '"text": "Here is the answer."' in body
    assert '"done": true' in body


@pytest.mark.django_db(transaction=True)
def test_error_is_surfaced_when_all_models_fail():
    async def _run():
        with patch("chat.views.build_agents", return_value=(_FailingAgent(), _FailingAgent())):
            response = await AsyncClient().post(
                "/chat/stream",
                data=json.dumps({"message": "hi"}),
                content_type="application/json",
            )
            return await _drain(response)

    body = asyncio.run(_run())
    assert '"error"' in body
    assert '"done": true' in body


# --- Admin-managed API keys -----------------------------------------------


def test_llm_credential_registered_in_admin():
    from chat.models import LLMCredential

    assert LLMCredential in django_admin.site._registry


@pytest.mark.django_db
def test_llm_credential_is_encrypted_at_rest():
    from django.db import connection

    from chat.models import LLMCredential

    cred = LLMCredential.objects.create(provider="groq", api_key="gsk_supersecret")

    # The model returns the plaintext key...
    assert LLMCredential.objects.get(pk=cred.pk).api_key == "gsk_supersecret"

    # ...but the raw database value is ciphertext, not the plaintext.
    with connection.cursor() as cursor:
        cursor.execute("SELECT api_key FROM chat_llmcredential WHERE id = %s", [cred.pk])
        raw = cursor.fetchone()[0]
    assert raw != "gsk_supersecret"
    assert "gsk_supersecret" not in raw


def test_build_agents_builds_one_agent_per_key():
    """Multiple keys for a provider each become an agent, in failover order."""
    from chat.agent import build_agents

    # Two Groq keys + Gemini falling back to its env var → 3 agents total.
    agents = build_agents({"groq": ["k1", "k2"]})
    assert len(agents) == 3


# --- Phase 5: rate limiting & guardrails ----------------------------------


@pytest.mark.django_db(transaction=True)
def test_rate_limit_blocks_after_the_limit():
    async def _run():
        statuses = []
        with (
            override_settings(CHAT_RATE_LIMIT=2, CHAT_RATE_WINDOW_SECONDS=600),
            patch("chat.views.build_agents", return_value=(_RecordingAgent(),)),
        ):
            client = AsyncClient()
            for _ in range(3):
                response = await client.post(
                    "/chat/stream",
                    data=json.dumps({"message": "hi"}),
                    content_type="application/json",
                )
                statuses.append(response.status_code)
                if response.status_code == 200:
                    await _drain(response)
        return statuses

    assert asyncio.run(_run()) == [200, 200, 429]


@pytest.mark.django_db(transaction=True)
def test_message_that_is_too_long_is_rejected():
    async def _run():
        with override_settings(CHAT_MAX_MESSAGE_LENGTH=10):
            return await AsyncClient().post(
                "/chat/stream",
                data=json.dumps({"message": "x" * 50}),
                content_type="application/json",
            )

    assert asyncio.run(_run()).status_code == 400


@pytest.mark.django_db(transaction=True)
def test_history_sent_to_the_model_is_bounded():
    recording = _RecordingAgent(reply="ok")

    async def _run():
        with (
            override_settings(CHAT_MAX_HISTORY_MESSAGES=2),
            patch("chat.views.build_agents", return_value=(recording,)),
        ):
            client = AsyncClient()
            conversation_id = None
            for i in range(4):
                data = {"message": f"msg{i}"}
                if conversation_id:
                    data["conversation_id"] = conversation_id
                response = await client.post(
                    "/chat/stream", data=json.dumps(data), content_type="application/json"
                )
                conversation_id = _conversation_id_from(await _drain(response))
        return recording.seen

    seen = asyncio.run(_run())
    assert all(len(messages) <= 2 for messages in seen)
    assert max(len(messages) for messages in seen) == 2  # the cap is actually reached
