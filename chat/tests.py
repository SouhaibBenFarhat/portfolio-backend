"""Tests for the chat app.

Phase 0 guards: app wiring, async stack, DB config.
Phase 1/3a guards: streaming through the LangGraph agent (a fake model, so no
API key is needed).
Phase 2 guards: conversation persistence and memory across messages.
"""

import asyncio
import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from django.conf import settings
from django.contrib import admin as django_admin
from django.test import AsyncClient, RequestFactory, override_settings
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


def _model_stream_event(text: str, model: str | None = None) -> dict:
    event = {
        "event": "on_chat_model_stream",
        "name": "model",
        "data": {"chunk": SimpleNamespace(content=text)},
    }
    if model:
        event["metadata"] = {"ls_model_name": model}
    return event


class _RecordingAgent:
    """Stands in for the compiled agent: records its inputs, streams a reply."""

    def __init__(self, reply: str = "ok", model: str | None = None):
        self.seen = []
        self._reply = reply
        self._model = model

    async def astream_events(self, payload, version=None):
        self.seen.append(payload["messages"])
        yield _model_stream_event(self._reply, self._model)


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


class _StreamsThenFailsAgent:
    """Streams a preamble token, then raises — mimics a model that writes "let me look
    that up", calls a tool, and errors on the step after it, once text has already shown.
    This is the case whose error used to be swallowed."""

    async def astream_events(self, payload, version=None):
        yield _model_stream_event("Let me look that up. ")
        raise RuntimeError("provider rate limit hit mid-turn")


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


class _EmptyAgent:
    """Stand-in that produces nothing at all (an empty model response)."""

    async def astream_events(self, payload, version=None):
        return
        yield  # unreachable; makes this an async generator


class _UsageAgent:
    """Stand-in that streams a token and reports usage, the way a real provider does.

    Emits two `on_chat_model_end` events to mirror a ReAct turn's two model calls (decide
    a tool, then answer with its result), with different prompt sizes.
    """

    def __init__(self, model: str = "mistral/mistral-small-latest"):
        self._model = model

    def _end(self, input_tokens: int) -> dict:
        return {
            "event": "on_chat_model_end",
            "name": "model",
            "data": {
                "output": SimpleNamespace(
                    content="",
                    usage_metadata={
                        "input_tokens": input_tokens,
                        "output_tokens": 7,
                        "total_tokens": input_tokens + 7,
                    },
                )
            },
            "metadata": {"ls_model_name": self._model},
        }

    async def astream_events(self, payload, version=None):
        yield _model_stream_event("Hi")
        yield self._end(120)  # first call: system prompt + history
        yield self._end(450)  # second call: the above plus the tool's result


async def _drain(response) -> str:
    parts = [chunk async for chunk in response.streaming_content]
    return b"".join(parts).decode()


def _usage_from(body: str) -> dict | None:
    """The payload of the stream's `usage` frame, or None if it wasn't sent."""
    for frame in body.split("\n\n"):
        line = frame.replace("data: ", "").strip()
        if not line:
            continue
        data = json.loads(line)
        if "usage" in data:
            return data["usage"]
    return None


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


@pytest.mark.django_db(transaction=True)
def test_conversation_detail_returns_messages_in_order():
    """The restore endpoint returns a conversation's messages, oldest first."""

    async def _run():
        from chat.models import Conversation, Message

        conv = await Conversation.objects.acreate()
        await Message.objects.acreate(conversation=conv, role="user", content="hi")
        await Message.objects.acreate(conversation=conv, role="assistant", content="hello!")
        res = await AsyncClient().get(f"/chat/conversations/{conv.id}/")
        return res.status_code, json.loads(res.content)

    status, data = asyncio.run(_run())
    assert status == 200
    assert [m["role"] for m in data["messages"]] == ["user", "assistant"]
    assert data["messages"][1]["content"] == "hello!"


@pytest.mark.django_db(transaction=True)
def test_conversation_detail_404_for_unknown_id():
    """An unknown (or wiped) conversation returns 404 so the client starts fresh."""
    import uuid as uuidlib

    async def _run():
        return await AsyncClient().get(f"/chat/conversations/{uuidlib.uuid4()}/")

    assert asyncio.run(_run()).status_code == 404


@pytest.mark.django_db(transaction=True)
def test_conversation_detail_rejects_unsupported_method():
    async def _run():
        from chat.models import Conversation

        conv = await Conversation.objects.acreate()
        return await AsyncClient().post(f"/chat/conversations/{conv.id}/")

    assert asyncio.run(_run()).status_code == 405


@pytest.mark.django_db(transaction=True)
def test_conversation_delete_removes_conversation_and_messages():
    """DELETE removes the conversation and cascade-deletes its messages."""

    async def _run():
        from chat.models import Conversation, Message

        conv = await Conversation.objects.acreate()
        await Message.objects.acreate(conversation=conv, role="user", content="hi")
        res = await AsyncClient().delete(f"/chat/conversations/{conv.id}/")
        remaining = await Conversation.objects.filter(id=conv.id).acount()
        messages_left = await Message.objects.acount()
        return res.status_code, remaining, messages_left

    status, remaining, messages_left = asyncio.run(_run())
    assert status == 204
    assert remaining == 0
    assert messages_left == 0  # cascade-deleted with the conversation


@pytest.mark.django_db(transaction=True)
def test_conversation_delete_404_for_unknown_id():
    import uuid as uuidlib

    async def _run():
        return await AsyncClient().delete(f"/chat/conversations/{uuidlib.uuid4()}/")

    assert asyncio.run(_run()).status_code == 404


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
    assert '"label": "loading facts"' in body  # the human-readable label rides along
    assert '"status": "start"' in body
    assert '"status": "end"' in body
    assert '"text": "Here you go"' in body
    assert '"done": true' in body


def test_tool_label_maps_known_names_and_falls_back():
    from chat.tools import tool_label

    assert tool_label("get_cv") == "reading the CV"
    assert tool_label("read_document") == "reading a document"
    assert tool_label("not_a_tool") == "working"  # unmapped tool never leaks a function name


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


@pytest.mark.django_db(transaction=True)
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


@pytest.mark.django_db(transaction=True)
def test_list_github_projects_tool_handles_rate_limit_gracefully():
    """A GitHub 403 (anonymous rate limit) returns a readable message, not an
    exception that would abort the whole chat turn."""
    import httpx

    from chat import tools

    class _Resp:
        status_code = 403

        def raise_for_status(self):
            raise httpx.HTTPStatusError(
                "rate limited",
                request=httpx.Request("GET", "https://api.github.com"),
                response=self,
            )

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
    assert "rate-limited" in result
    assert "try again" in result


@pytest.mark.django_db(transaction=True)
def test_github_token_prefers_admin_credential_over_env():
    """A provider="github" credential in the admin is used for the GitHub token,
    taking precedence over the GITHUB_TOKEN env var."""
    from chat import tools
    from chat.models import LLMCredential

    async def _run():
        await LLMCredential.objects.acreate(provider="github", api_key="ghp_admintoken")
        return await tools._github_token()

    with override_settings(GITHUB_TOKEN="env_token"):
        assert asyncio.run(_run()) == "ghp_admintoken"


def test_github_headers_carry_bearer_token():
    from chat import tools

    assert tools._github_headers("ghp_x")["Authorization"] == "Bearer ghp_x"
    assert "Authorization" not in tools._github_headers("")  # anonymous


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
def test_empty_response_regenerates_with_the_next_model():
    """An empty answer from the first model falls over to the next model."""

    async def _run():
        agents = (_EmptyAgent(), _RecordingAgent(reply="from the retry"))
        with patch("chat.views.build_agents", return_value=agents):
            response = await AsyncClient().post(
                "/chat/stream",
                data=json.dumps({"message": "hi"}),
                content_type="application/json",
            )
            return await _drain(response)

    body = asyncio.run(_run())
    assert '"error"' not in body
    assert '"text": "from the retry"' in body


@pytest.mark.django_db(transaction=True)
def test_graceful_line_when_every_model_returns_empty():
    """If all retries are empty (rare), a graceful line is sent — never a blank bubble."""

    async def _run():
        with patch("chat.views.build_agents", return_value=(_EmptyAgent(), _EmptyAgent())):
            response = await AsyncClient().post(
                "/chat/stream",
                data=json.dumps({"message": "hi"}),
                content_type="application/json",
            )
            return await _drain(response)

    body = asyncio.run(_run())
    assert "couldn't find an answer" in body
    assert '"done": true' in body


def _error_frame_from(body: str) -> dict | None:
    """The payload of the stream's error frame, or None if there wasn't one."""
    for frame in body.split("\n\n"):
        line = frame.replace("data: ", "").strip()
        if not line:
            continue
        data = json.loads(line)
        if "error" in data:
            return data
    return None


@pytest.mark.django_db(transaction=True)
def test_error_is_surfaced_when_all_models_fail():
    from chat.views import CHAT_ERROR_MESSAGE

    async def _run():
        with patch("chat.views.build_agents", return_value=(_FailingAgent(), _FailingAgent())):
            response = await AsyncClient().post(
                "/chat/stream",
                data=json.dumps({"message": "hi"}),
                content_type="application/json",
            )
            return await _drain(response)

    body = asyncio.run(_run())
    frame = _error_frame_from(body)
    assert frame is not None
    assert frame["error"] == CHAT_ERROR_MESSAGE  # the friendly line, safe to show anyone
    assert "rate limit exceeded" in frame["detail"]  # the raw cause rides alongside it
    assert '"done": true' in body


@pytest.mark.django_db(transaction=True)
def test_error_after_a_preamble_is_surfaced_not_swallowed():
    """The reported bug: a model streams "let me look that up", calls a tool, then the
    next step fails — the client used to be left with a half-answer and no error. Now the
    error frame is sent even though text already streamed."""
    from chat.views import CHAT_ERROR_MESSAGE

    async def _run():
        with patch("chat.views.build_agents", return_value=(_StreamsThenFailsAgent(),)):
            response = await AsyncClient().post(
                "/chat/stream",
                data=json.dumps({"message": "who is souhaib?"}),
                content_type="application/json",
            )
            return await _drain(response)

    body = asyncio.run(_run())
    assert "Let me look that up." in body  # the partial answer that did stream is kept
    frame = _error_frame_from(body)
    assert frame is not None  # ...and the failure is no longer swallowed
    assert frame["error"] == CHAT_ERROR_MESSAGE
    assert "provider rate limit hit mid-turn" in frame["detail"]  # real cause, for the owner
    assert "rate limit" not in frame["error"]  # but never in the line shown to visitors
    assert '"done": true' in body


@pytest.mark.django_db(transaction=True)
def test_a_broken_turn_is_not_persisted_as_a_reply():
    """A half-answer from a failed turn must not be saved — on reload it would read as a
    complete reply that stops for no reason."""

    async def _run():
        from chat.models import Message

        with patch("chat.views.build_agents", return_value=(_StreamsThenFailsAgent(),)):
            response = await AsyncClient().post(
                "/chat/stream",
                data=json.dumps({"message": "who is souhaib?"}),
                content_type="application/json",
            )
            await _drain(response)
        return [m.role async for m in Message.objects.all()]

    # The user's question is saved; the broken partial answer is not.
    assert asyncio.run(_run()) == ["user"]


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


def test_build_agents_builds_one_agent_per_model_and_key():
    """Each (model, key) pair becomes an agent, in failover order."""
    from chat.agent import build_agents

    # Two models (primary + fallback are both Mistral) × two Mistral keys → 4 agents.
    agents = build_agents({"mistral": ["k1", "k2"]})
    assert len(agents) == 4


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


def test_client_ip_prefers_cloudflare_header_over_forwarded_for():
    """The rate-limit key is Cloudflare's real client IP (CF-Connecting-IP), never the
    spoofable X-Forwarded-For, whose leftmost entry the client controls."""
    from chat.views import _client_ip

    request = RequestFactory().post(
        "/chat/stream",
        HTTP_CF_CONNECTING_IP="203.0.113.7",
        HTTP_X_FORWARDED_FOR="1.2.3.4",  # spoofable — must be ignored when the CF header is set
    )
    assert _client_ip(request) == "203.0.113.7"


def test_client_ip_falls_back_to_remote_addr_locally():
    """No Cloudflare in front (local dev) → REMOTE_ADDR, so the key is still stable."""
    from chat.views import _client_ip

    request = RequestFactory().post("/chat/stream", REMOTE_ADDR="198.51.100.9")
    assert _client_ip(request) == "198.51.100.9"


@pytest.mark.django_db(transaction=True)
def test_rate_limit_key_ignores_spoofed_forwarded_for():
    """An abuser rotating X-Forwarded-For can't mint fresh rate-limit buckets: the key
    comes from Cloudflare's CF-Connecting-IP, which the client can't forge."""

    async def _run():
        statuses = []
        with (
            override_settings(CHAT_RATE_LIMIT=2, CHAT_RATE_WINDOW_SECONDS=60),
            patch("chat.views.build_agents", return_value=(_RecordingAgent(),)),
        ):
            client = AsyncClient()
            for i in range(3):
                response = await client.post(
                    "/chat/stream",
                    data=json.dumps({"message": "hi"}),
                    content_type="application/json",
                    HTTP_CF_CONNECTING_IP="203.0.113.7",  # same real client every time
                    HTTP_X_FORWARDED_FOR=f"10.0.0.{i}",  # rotating spoof — must not create new keys
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


# --- Context-window usage --------------------------------------------------


def _post_and_drain(agents: tuple) -> str:
    async def _run():
        with patch("chat.views.build_agents", return_value=agents):
            response = await AsyncClient().post(
                "/chat/stream",
                data=json.dumps({"message": "hi"}),
                content_type="application/json",
            )
            return await _drain(response)

    return asyncio.run(_run())


@pytest.mark.django_db(transaction=True)
def test_usage_frame_reports_the_largest_prompt_of_the_turn():
    """A turn makes several model calls; the gauge reports the fullest prompt, not the
    first one and not the sum."""
    with override_settings(CHAT_MAX_CONTEXT_TOKENS=20000):
        usage = _usage_from(_post_and_drain((_UsageAgent(),)))
    assert usage["context_tokens"] == 450
    assert usage["context_limit"] == 20000
    assert usage["exhausted"] is False


@pytest.mark.django_db(transaction=True)
def test_usage_frame_is_sent_before_done():
    """The client can rely on `usage` arriving while the stream is still open."""
    body = _post_and_drain((_UsageAgent(),))
    assert '"usage"' in body
    assert body.index('"usage"') < body.index('"done"')


@pytest.mark.django_db(transaction=True)
def test_no_usage_frame_when_the_provider_reports_none():
    """No usage reported → no frame, so the client never renders a gauge from nothing."""
    body = _post_and_drain((_RecordingAgent(),))
    assert _usage_from(body) is None
    assert '"done": true' in body


# --- Streaming the answering model's name ----------------------------------


def _model_from(body: str) -> str | None:
    """The `model` id the stream named, or None if no model frame was sent."""
    for frame in body.split("\n\n"):
        line = frame.replace("data: ", "").strip()
        if not line:
            continue
        data = json.loads(line)
        if "model" in data:
            return data["model"]
    return None


@pytest.mark.django_db(transaction=True)
def test_stream_names_the_answering_model_before_the_reply():
    """The model handling the turn is named in a `model` frame, before its text, so the
    client can show who's replying (it maps the id to a display name itself)."""
    body = _post_and_drain((_RecordingAgent(reply="Hello", model="mistral/mistral-small-latest"),))
    assert _model_from(body) == "mistral/mistral-small-latest"
    assert body.index('"model"') < body.index('"text"')


@pytest.mark.django_db(transaction=True)
def test_model_frame_names_the_model_that_replies_after_failover():
    """On failover the named model is the one that answers, not the one that failed."""
    fallback = _RecordingAgent(reply="from the fallback", model="mistral/open-mistral-nemo")
    body = _post_and_drain((_FailingAgent(), fallback))
    assert '"error"' not in body
    assert _model_from(body) == "mistral/open-mistral-nemo"


@pytest.mark.django_db(transaction=True)
def test_empty_attempt_is_not_named_only_the_reply_is():
    """An empty first attempt is never named — only the model that produces the reply."""
    agents = (
        _EmptyAgent(),
        _RecordingAgent(reply="from the retry", model="mistral/mistral-small-latest"),
    )
    assert _model_from(_post_and_drain(agents)) == "mistral/mistral-small-latest"


@pytest.mark.django_db(transaction=True)
def test_no_model_frame_when_the_provider_reports_no_model():
    """No model id reported → no frame, mirroring the usage frame's omission."""
    body = _post_and_drain((_RecordingAgent(reply="hi"),))
    assert _model_from(body) is None
    assert '"text": "hi"' in body
    assert '"done": true' in body


def test_context_limit_reads_the_model_table():
    from chat.agent import context_limit

    assert context_limit("mistral/mistral-small-latest") == 131072
    assert context_limit("not-a-real/model") == 0  # unknown model can't break a turn


def test_token_budget_never_exceeds_the_models_window():
    """A cap set above what the model can read would wedge every chat — it's clamped."""
    from chat.views import _token_budget

    with override_settings(CHAT_MAX_CONTEXT_TOKENS=20000):
        assert _token_budget("mistral/mistral-small-latest") == 20000  # our cap is lower
    with override_settings(CHAT_MAX_CONTEXT_TOKENS=999_999):
        assert _token_budget("mistral/mistral-small-latest") == 131072  # clamped to the model
    with override_settings(CHAT_MAX_CONTEXT_TOKENS=20000):
        assert _token_budget("not-a-real/model") == 20000  # unknown window → our cap stands


# --- Context budget: the thread is disabled once it's spent -----------------


@pytest.mark.django_db(transaction=True)
def test_context_tokens_are_persisted_on_the_conversation():
    """The gauge must survive a reload, so the turn's context size is stored."""

    async def _run():
        from chat.models import Conversation

        with patch("chat.views.build_agents", return_value=(_UsageAgent(),)):
            response = await AsyncClient().post(
                "/chat/stream",
                data=json.dumps({"message": "hi"}),
                content_type="application/json",
            )
            body = await _drain(response)
        conversation = await Conversation.objects.aget(id=_conversation_id_from(body))
        return conversation.context_tokens

    assert asyncio.run(_run()) == 450  # overwritten each turn, not summed


@pytest.mark.django_db(transaction=True)
def test_spent_conversation_refuses_new_messages():
    """Past the budget the chat is disabled: 403, and no model call is made."""

    async def _run():
        from chat.models import Conversation

        conversation = await Conversation.objects.acreate(context_tokens=20000)
        with override_settings(CHAT_MAX_CONTEXT_TOKENS=20000):
            response = await AsyncClient().post(
                "/chat/stream",
                data=json.dumps({"message": "hi", "conversation_id": str(conversation.id)}),
                content_type="application/json",
            )
        return response.status_code, json.loads(response.content)

    status, data = asyncio.run(_run())
    assert status == 403
    assert data["usage"]["exhausted"] is True


@pytest.mark.django_db(transaction=True)
def test_a_fresh_conversation_is_never_refused():
    """The budget is per thread — starting a new chat always works."""

    async def _run():
        with (
            override_settings(CHAT_MAX_CONTEXT_TOKENS=20000),
            patch("chat.views.build_agents", return_value=(_UsageAgent(),)),
        ):
            response = await AsyncClient().post(
                "/chat/stream",
                data=json.dumps({"message": "hi"}),
                content_type="application/json",
            )
            await _drain(response)
            return response.status_code

    assert asyncio.run(_run()) == 200


@pytest.mark.django_db(transaction=True)
def test_restore_returns_usage_so_the_gauge_survives_a_reload():
    async def _run():
        from chat.models import Conversation, Message

        conversation = await Conversation.objects.acreate(context_tokens=4200)
        await Message.objects.acreate(conversation=conversation, role="user", content="hi")
        with override_settings(CHAT_MAX_CONTEXT_TOKENS=20000):
            res = await AsyncClient().get(f"/chat/conversations/{conversation.id}/")
        return json.loads(res.content)

    data = asyncio.run(_run())
    assert data["usage"] == {
        "context_tokens": 4200,
        "context_limit": 20000,
        "exhausted": False,
    }


# --- Token usage / consumption --------------------------------------------


def test_token_usage_registered_in_admin():
    from chat.models import TokenUsage

    assert TokenUsage in django_admin.site._registry


@pytest.mark.django_db(transaction=True)
def test_token_usage_records_summed_input_and_output_per_model():
    """Consumption sums every model call's input+output for the turn — not the max the
    gauge keeps. _UsageAgent makes two calls (120 and 450 input, 7 output each)."""

    async def _run():
        from chat.models import TokenUsage

        with patch("chat.views.build_agents", return_value=(_UsageAgent(),)):
            response = await AsyncClient().post(
                "/chat/stream",
                data=json.dumps({"message": "hi"}),
                content_type="application/json",
            )
            await _drain(response)
        row = await TokenUsage.objects.aget(model="mistral/mistral-small-latest")
        return row.input_tokens, row.output_tokens, row.total_tokens

    assert asyncio.run(_run()) == (570, 14, 584)  # 120+450 in, 7+7 out


@pytest.mark.django_db(transaction=True)
def test_token_usage_accumulates_across_turns():
    """The counter is cumulative: a second turn adds to the month's running total
    rather than overwriting it (that's what makes it a consumption odometer)."""

    async def _run():
        from chat.models import TokenUsage

        with patch("chat.views.build_agents", return_value=(_UsageAgent(),)):
            client = AsyncClient()
            first = await client.post(
                "/chat/stream",
                data=json.dumps({"message": "hi"}),
                content_type="application/json",
            )
            conversation_id = _conversation_id_from(await _drain(first))
            second = await client.post(
                "/chat/stream",
                data=json.dumps({"message": "again", "conversation_id": conversation_id}),
                content_type="application/json",
            )
            await _drain(second)
        row = await TokenUsage.objects.aget(model="mistral/mistral-small-latest")
        return row.input_tokens, row.output_tokens

    assert asyncio.run(_run()) == (1140, 28)  # two turns × (570 in, 14 out)


# --- Scope check (is this question ours to answer?) -------------------------


def _fake_guard(verdict: str):
    class _FakeGuardModel:
        async def ainvoke(self, messages):
            _FakeGuardModel.seen = messages
            return SimpleNamespace(content=verdict)

    return _FakeGuardModel()


def test_scope_check_maps_in_and_out_verdicts_to_bool():
    """is_message_in_scope turns the model's IN/OUT word into a bool."""
    from chat import guard

    async def _run(verdict):
        with patch.object(guard, "build_guard_model", return_value=_fake_guard(verdict)):
            return await guard.is_message_in_scope("what are his skills?", api_key="test-key")

    assert asyncio.run(_run("IN")) is True
    assert asyncio.run(_run("OUT")) is False


def test_scope_check_is_shown_the_previous_reply_so_follow_ups_make_sense():
    """ "tell me more" is only judgeable against what came before — on its own it looks
    like a question about nothing."""
    from chat import guard

    captured = {}

    class _Spy:
        async def ainvoke(self, messages):
            captured["prompt"] = messages[-1].content
            return SimpleNamespace(content="IN")

    async def _run():
        with patch.object(guard, "build_guard_model", return_value=_Spy()):
            return await guard.is_message_in_scope(
                "tell me more", previous_reply="Souhaib built a Django backend.", api_key="k"
            )

    assert asyncio.run(_run()) is True
    assert "Django backend" in captured["prompt"]  # the context reached the model
    assert "tell me more" in captured["prompt"]


def test_scope_check_allows_when_no_key_configured():
    """With no key the check can't run — it fails open so the chat still works."""
    from chat import guard

    with patch.object(guard, "_guard_key", return_value=""):
        assert asyncio.run(guard.is_message_in_scope("anything", api_key=None)) is True


def test_scope_check_fails_open_on_error():
    """A failed check must not refuse a real recruiter — better a wasted answer."""
    from chat import guard

    def _boom(_key):
        raise RuntimeError("guard down")

    with patch.object(guard, "build_guard_model", side_effect=_boom):
        assert asyncio.run(guard.is_message_in_scope("what are his skills?", api_key="k")) is True


@pytest.mark.django_db(transaction=True)
def test_off_topic_question_never_reaches_the_model():
    """The whole point: someone using the chat as a free coding tutor costs one short
    classification, not a generated answer plus the thread resent as input."""
    agent = _RecordingAgent(reply="Here is how you write a for loop in Python...")

    async def _run():
        with (
            patch("chat.views.build_agents", return_value=(agent,)),
            patch("chat.views.is_message_in_scope", new=AsyncMock(return_value=False)),
        ):
            response = await AsyncClient().post(
                "/chat/stream",
                data=json.dumps({"message": "teach me python"}),
                content_type="application/json",
            )
            return await _drain(response)

    body = asyncio.run(_run())
    assert agent.seen == []  # the expensive model was never invoked — that's the saving
    assert "for loop" not in body
    assert "just here to talk about Souhaib" in body  # a redirect, streamed like a reply
    assert '"done": true' in body


@pytest.mark.django_db(transaction=True)
def test_a_refused_message_is_saved_so_the_thread_survives_a_reload():
    """The redirect is a real turn in the conversation, not a phantom."""

    async def _run():
        from chat.models import Message

        with (
            patch("chat.views.build_agents", return_value=(_RecordingAgent(),)),
            patch("chat.views.is_message_in_scope", new=AsyncMock(return_value=False)),
        ):
            response = await AsyncClient().post(
                "/chat/stream",
                data=json.dumps({"message": "write me an essay"}),
                content_type="application/json",
            )
            await _drain(response)
        return [(m.role, m.content[:30]) async for m in Message.objects.all()]

    roles = asyncio.run(_run())
    assert [r for r, _ in roles] == ["user", "assistant"]
    assert "just here to talk" in roles[1][1]


@pytest.mark.django_db(transaction=True)
def test_an_in_scope_question_is_answered_normally():
    """A real question streams the model's own words, untouched."""

    async def _run():
        with (
            patch(
                "chat.views.build_agents", return_value=(_RecordingAgent(reply="He knows Django!"),)
            ),
            patch("chat.views.is_message_in_scope", new=AsyncMock(return_value=True)),
        ):
            response = await AsyncClient().post(
                "/chat/stream",
                data=json.dumps({"message": "does he know django?"}),
                content_type="application/json",
            )
            return await _drain(response)

    body = asyncio.run(_run())
    assert '"text": "He knows Django!"' in body
    assert "just here to talk about Souhaib" not in body


@pytest.mark.django_db(transaction=True)
def test_scope_check_can_be_disabled():
    """With CHAT_GUARD_ENABLED off the check never runs and everything is answered."""
    guard_spy = AsyncMock(return_value=False)  # would refuse everything, if called

    async def _run():
        agent = _RecordingAgent(reply="anything at all here.")
        with (
            override_settings(CHAT_GUARD_ENABLED=False),
            patch("chat.views.build_agents", return_value=(agent,)),
            patch("chat.views.is_message_in_scope", new=guard_spy),
        ):
            response = await AsyncClient().post(
                "/chat/stream",
                data=json.dumps({"message": "hi"}),
                content_type="application/json",
            )
            return await _drain(response)

    body = asyncio.run(_run())
    assert "anything at all here." in body  # answered despite the refusing check...
    assert guard_spy.await_count == 0  # ...because it was never called


# --- Document upload + document tools ---------------------------------------


def _tiny_pdf(text: str) -> bytes:
    """A minimal one-page PDF containing `text`, assembled by hand with a correct
    xref table (pypdf needs one to parse) — no PDF-writing dependency required."""
    stream = f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF"
    ).encode()
    return bytes(out)


def _tiny_docx(text: str) -> bytes:
    from io import BytesIO

    from docx import Document as DocxDocument

    buffer = BytesIO()
    docx = DocxDocument()
    docx.add_paragraph(text)
    docx.save(buffer)
    return buffer.getvalue()


def test_extract_text_from_pdf():
    from chat.extraction import extract_text

    assert "Hello PDF" in extract_text(_tiny_pdf("Hello PDF"), "cv.pdf")


def test_extract_text_from_docx():
    from chat.extraction import extract_text

    assert "Ten years of Django" in extract_text(_tiny_docx("Ten years of Django"), "cv.docx")


def test_extract_text_from_plain_text():
    from chat.extraction import extract_text

    assert extract_text(b"plain words", "notes.txt") == "plain words"


def test_extract_rejects_unsupported_file_type():
    from chat.extraction import extract_text

    with pytest.raises(ValueError, match="Unsupported file type"):
        extract_text(b"x", "cv.exe")


def test_extract_rejects_a_pdf_with_no_text():
    """A scanned/image-only PDF yields no text — a readable error, not silent emptiness."""
    from io import BytesIO

    from pypdf import PdfWriter

    from chat.extraction import extract_text

    buffer = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(buffer)
    with pytest.raises(ValueError, match="No text"):
        extract_text(buffer.getvalue(), "scan.pdf")


@pytest.mark.django_db
def test_admin_upload_extracts_text_and_stores_blob():
    """Uploading a file fills `content` with its text (for the agent) and keeps the
    original bytes (for the admin preview)."""
    from django.core.files.uploadedfile import SimpleUploadedFile

    from chat.admin import DocumentAdmin, DocumentAdminForm
    from chat.models import Document

    pdf = _tiny_pdf("Ten years of Django")
    form = DocumentAdminForm(
        data={"slug": "cv", "title": "CV", "content": "", "is_active": "on"},
        files={"upload": SimpleUploadedFile("cv.pdf", pdf, content_type="application/pdf")},
    )
    assert form.is_valid(), form.errors
    obj = form.save(commit=False)
    DocumentAdmin(Document, django_admin.site).save_model(None, obj, form, change=False)

    saved = Document.objects.get(slug="cv")
    assert "Ten years of Django" in saved.content
    assert bytes(saved.file_data) == pdf
    assert saved.file_content_type == "application/pdf"
    assert saved.file_name == "cv.pdf"
    assert saved.file_uploaded_at is not None


@pytest.mark.django_db
def test_admin_form_requires_content_or_a_file():
    from chat.admin import DocumentAdminForm

    form = DocumentAdminForm(data={"slug": "cv", "title": "CV", "content": "", "is_active": "on"})
    assert not form.is_valid()
    assert "content" in form.errors


@pytest.mark.django_db
def test_admin_form_rejects_an_oversized_file():
    from django.core.files.uploadedfile import SimpleUploadedFile

    from chat.admin import DocumentAdminForm

    big = SimpleUploadedFile("cv.pdf", b"x" * (10 * 1024 * 1024 + 1))
    form = DocumentAdminForm(
        data={"slug": "cv", "title": "CV", "content": "", "is_active": "on"},
        files={"upload": big},
    )
    assert not form.is_valid()
    assert "too large" in str(form.errors["upload"])


@pytest.mark.django_db
def test_document_file_is_served_to_staff(client):
    from django.contrib.auth.models import User

    from chat.models import Document

    client.force_login(User.objects.create_superuser("admin", "a@example.com", "pw"))
    doc = Document.objects.create(
        slug="cv",
        title="CV",
        content="x",
        file_data=b"%PDF-fake",
        file_name="cv.pdf",
        file_content_type="application/pdf",
    )
    response = client.get(f"/admin/chat/document/{doc.pk}/file/")
    assert response.status_code == 200
    assert response["Content-Type"] == "application/pdf"
    assert response.content == b"%PDF-fake"
    assert "inline" in response["Content-Disposition"]


@pytest.mark.django_db
def test_document_admin_pages_render_with_upload_and_preview(client):
    """The add page carries the upload field and the change page shows the PDF preview
    iframe — catches template/form breakage the form-level tests can't see."""
    from django.contrib.auth.models import User

    from chat.models import Document

    client.force_login(User.objects.create_superuser("admin", "a@example.com", "pw"))
    add_page = client.get("/admin/chat/document/add/")
    assert add_page.status_code == 200
    assert b'name="upload"' in add_page.content

    doc = Document.objects.create(
        slug="cv",
        title="CV",
        content="x",
        file_data=b"%PDF-fake",
        file_name="cv.pdf",
        file_content_type="application/pdf",
    )
    change_page = client.get(f"/admin/chat/document/{doc.pk}/change/")
    assert change_page.status_code == 200
    assert b"<iframe" in change_page.content
    assert f"/admin/chat/document/{doc.pk}/file/".encode() in change_page.content


@pytest.mark.django_db
def test_document_file_requires_admin_login(client):
    """The blob is admin-only — anonymous requests are sent to the login page."""
    from chat.models import Document

    doc = Document.objects.create(slug="cv", title="CV", content="x", file_data=b"d")
    response = client.get(f"/admin/chat/document/{doc.pk}/file/")
    assert response.status_code == 302
    assert "/admin/login/" in response["Location"]


@pytest.mark.django_db(transaction=True)
def test_list_documents_tool_lists_only_active_documents():
    from chat.models import Document
    from chat.tools import list_documents

    async def _run():
        await Document.objects.acreate(slug="cv", title="Résumé", content="…")
        await Document.objects.acreate(
            slug="old-letter", title="Old letter", content="…", is_active=False
        )
        return await list_documents.ainvoke({})

    result = asyncio.run(_run())
    assert "cv: Résumé" in result
    assert "old-letter" not in result  # inactive documents are excluded


@pytest.mark.django_db(transaction=True)
def test_read_document_tool_returns_the_content():
    from chat.models import Document
    from chat.tools import read_document

    async def _run():
        await Document.objects.acreate(
            slug="cover-letter", title="Cover letter", content="I build backends."
        )
        return await read_document.ainvoke({"slug": "cover-letter"})

    assert "I build backends." in asyncio.run(_run())


@pytest.mark.django_db(transaction=True)
def test_read_document_tool_handles_an_unknown_slug():
    from chat.tools import read_document

    async def _run():
        return await read_document.ainvoke({"slug": "nope"})

    assert "No document named 'nope'" in asyncio.run(_run())


@pytest.mark.django_db(transaction=True)
def test_read_document_output_is_capped():
    """A very long document can't blow the context window through the tool."""
    from chat.models import Document
    from chat.tools import read_document

    async def _run():
        await Document.objects.acreate(slug="book", title="Book", content="x" * 7000)
        return await read_document.ainvoke({"slug": "book"})

    assert len(asyncio.run(_run())) == 6000


def test_document_tools_are_wired_into_the_agent():
    from chat.agent import TOOLS

    names = {tool.name for tool in TOOLS}
    assert {"list_documents", "read_document"} <= names


@pytest.mark.django_db(transaction=True)
def test_read_document_tool_excludes_inactive_documents():
    from chat.models import Document
    from chat.tools import read_document

    async def _run():
        await Document.objects.acreate(
            slug="old-letter", title="Old letter", content="stale", is_active=False
        )
        return await read_document.ainvoke({"slug": "old-letter"})

    assert "No document named" in asyncio.run(_run())  # inactive documents are excluded


def test_document_tools_defer_the_file_blob():
    """The tools only read text — pulling a multi-MB upload out of Postgres on every
    chat turn would spike the single 512MB worker."""
    from chat.tools import _documents

    deferred, _ = _documents().query.deferred_loading
    assert "file_data" in deferred


@pytest.mark.django_db
def test_admin_changelist_defers_the_file_blob(rf):
    """Same reasoning as the tools: listing documents must not load every blob."""
    from django.contrib.auth.models import User

    from chat.admin import DocumentAdmin
    from chat.models import Document

    request = rf.get("/admin/chat/document/")
    request.user = User.objects.create_superuser("admin", "a@example.com", "pw")
    queryset = DocumentAdmin(Document, django_admin.site).get_queryset(request)
    deferred, _ = queryset.query.deferred_loading
    assert "file_data" in deferred


@pytest.mark.django_db
def test_admin_edit_without_new_upload_preserves_the_blob():
    """Hand-fixing the extracted text (the documented workflow) must not lose the file."""
    from chat.admin import DocumentAdmin, DocumentAdminForm
    from chat.models import Document

    doc = Document.objects.create(
        slug="cv", title="CV", content="rough text", file_data=b"%PDF-orig", file_name="cv.pdf"
    )
    form = DocumentAdminForm(
        data={"slug": "cv", "title": "CV", "content": "fixed text", "is_active": "on"},
        instance=doc,
    )
    assert form.is_valid(), form.errors
    obj = form.save(commit=False)
    DocumentAdmin(Document, django_admin.site).save_model(None, obj, form, change=True)

    saved = Document.objects.get(slug="cv")
    assert saved.content == "fixed text"
    assert bytes(saved.file_data) == b"%PDF-orig"
    assert saved.file_name == "cv.pdf"


@pytest.mark.django_db
def test_admin_reupload_replaces_content_and_blob():
    from django.core.files.uploadedfile import SimpleUploadedFile

    from chat.admin import DocumentAdmin, DocumentAdminForm
    from chat.models import Document

    doc = Document.objects.create(
        slug="cv", title="CV", content="old", file_data=b"old-bytes", file_name="old.pdf"
    )
    form = DocumentAdminForm(
        data={"slug": "cv", "title": "CV", "content": "old", "is_active": "on"},
        files={"upload": SimpleUploadedFile("new.txt", b"brand new text")},
        instance=doc,
    )
    assert form.is_valid(), form.errors
    obj = form.save(commit=False)
    DocumentAdmin(Document, django_admin.site).save_model(None, obj, form, change=True)

    saved = Document.objects.get(slug="cv")
    assert saved.content == "brand new text"
    assert bytes(saved.file_data) == b"brand new text"
    assert saved.file_name == "new.txt"


@pytest.mark.django_db
def test_admin_remove_file_clears_the_blob_but_keeps_content():
    from chat.admin import DocumentAdmin, DocumentAdminForm
    from chat.models import Document

    doc = Document.objects.create(
        slug="cv",
        title="CV",
        content="keep me",
        file_data=b"bytes",
        file_name="cv.pdf",
        file_content_type="application/pdf",
    )
    form = DocumentAdminForm(
        data={
            "slug": "cv",
            "title": "CV",
            "content": "keep me",
            "is_active": "on",
            "remove_file": "on",
        },
        instance=doc,
    )
    assert form.is_valid(), form.errors
    obj = form.save(commit=False)
    DocumentAdmin(Document, django_admin.site).save_model(None, obj, form, change=True)

    saved = Document.objects.get(slug="cv")
    assert saved.content == "keep me"
    assert saved.file_data is None
    assert saved.file_name == ""
    assert saved.file_content_type == ""
    assert saved.file_uploaded_at is None


@pytest.mark.django_db
def test_uploaded_filename_control_characters_are_stripped():
    """A CR/LF smuggled into a filename would make every later Content-Disposition
    header raise BadHeaderError — a permanent 500 on the preview."""
    from django.core.files.uploadedfile import SimpleUploadedFile

    from chat.admin import DocumentAdmin, DocumentAdminForm
    from chat.models import Document

    form = DocumentAdminForm(
        data={"slug": "cv", "title": "CV", "content": "", "is_active": "on"},
        files={"upload": SimpleUploadedFile("a\r\nSet-Cookie: x.txt", b"hi")},
    )
    assert form.is_valid(), form.errors
    obj = form.save(commit=False)
    DocumentAdmin(Document, django_admin.site).save_model(None, obj, form, change=False)

    assert Document.objects.get(slug="cv").file_name == "aSet-Cookie: x.txt"


@pytest.mark.django_db
def test_document_file_handles_unicode_filenames(client):
    """Non-latin-1 names (a Word-export en-dash) must not mangle the header — the
    RFC 5987 filename* form is used instead of a bare f-string."""
    from django.contrib.auth.models import User

    from chat.models import Document

    client.force_login(User.objects.create_superuser("admin", "a@example.com", "pw"))
    doc = Document.objects.create(
        slug="cv",
        title="CV",
        content="x",
        file_data=b"%PDF-fake",
        file_name="CV – Souhaib.pdf",
        file_content_type="application/pdf",
    )
    response = client.get(f"/admin/chat/document/{doc.pk}/file/")
    assert response.status_code == 200
    assert "filename*=utf-8''" in response["Content-Disposition"]
    assert response["X-Frame-Options"] == "SAMEORIGIN"  # keeps the preview iframe working


@pytest.mark.django_db
def test_document_file_404_when_no_file_uploaded(client):
    from django.contrib.auth.models import User

    from chat.models import Document

    client.force_login(User.objects.create_superuser("admin", "a@example.com", "pw"))
    doc = Document.objects.create(slug="cv", title="CV", content="paste-only, no file")
    assert client.get(f"/admin/chat/document/{doc.pk}/file/").status_code == 404


@pytest.mark.django_db
def test_document_file_denied_to_staff_without_permission(client):
    """admin_view() only checks is_staff — the view must also enforce the Document
    view permission, or any staff account could fetch every uploaded blob."""
    from django.contrib.auth.models import User

    from chat.models import Document

    client.force_login(User.objects.create_user("limited", "l@example.com", "pw", is_staff=True))
    doc = Document.objects.create(slug="cv", title="CV", content="x", file_data=b"secret")
    assert client.get(f"/admin/chat/document/{doc.pk}/file/").status_code == 403


def test_extract_rejects_corrupt_bytes():
    """Garbage with a supported extension becomes a readable form error, not a crash."""
    from chat.extraction import extract_text

    with pytest.raises(ValueError, match="Could not read"):
        extract_text(b"not a real pdf at all", "cv.pdf")


# --- The failover chain, ordered in the admin -------------------------------


def test_chain_falls_back_to_the_env_vars_when_no_models_are_configured():
    """A wiped free-tier database, or a fresh deploy, must not take the chat down."""
    from chat.agent import resolve_chain

    with override_settings(
        CHAT_MODEL="mistral/mistral-small-latest",
        CHAT_FALLBACK_MODEL="mistral/open-mistral-nemo",
    ):
        assert resolve_chain([]) == [
            "mistral/mistral-small-latest",
            "mistral/open-mistral-nemo",
        ]
        assert resolve_chain(["zai/glm-5.2"]) == ["zai/glm-5.2"]  # a configured chain wins


def test_primary_model_is_the_head_of_the_chain():
    from chat.agent import primary_model

    assert primary_model(["zai/glm-5.2", "mistral/mistral-small-latest"]) == "zai/glm-5.2"
    with override_settings(CHAT_MODEL="mistral/mistral-small-latest"):
        assert primary_model([]) == "mistral/mistral-small-latest"


def test_build_agents_follows_the_chain_order():
    """The chain's order is the failover order, and each key multiplies its model."""
    from chat.agent import build_agents

    agents = build_agents(
        {"mistral": ["k1", "k2"]}, ["zai/glm-4.7-flash", "mistral/open-mistral-nemo"]
    )
    # GLM has no key (one agent, env-var key); Nemo has two admin keys → two agents.
    assert len(agents) == 3


def test_reordering_the_chain_rebuilds_the_agents():
    """Dragging a row must take effect on the next request — the cache keys off the plan,
    so a new order is a new plan, not a stale hit."""
    from chat.agent import build_agents

    first = build_agents({}, ["mistral/mistral-small-latest", "zai/glm-4.7-flash"])
    same = build_agents({}, ["mistral/mistral-small-latest", "zai/glm-4.7-flash"])
    swapped = build_agents({}, ["zai/glm-4.7-flash", "mistral/mistral-small-latest"])
    assert first is same  # unchanged chain → cached
    assert swapped is not first  # reordered chain → rebuilt


@pytest.mark.django_db(transaction=True)
def test_stream_runs_the_chain_in_the_admins_order():
    """The order dragged in the admin is the order the stream tries models in."""
    seen = []

    def _spy(provider_keys, model_ids=()):
        seen.append(list(model_ids))
        return (_RecordingAgent(),)

    async def _run():
        from chat.models import ChatModel

        await ChatModel.objects.all().adelete()
        await ChatModel.objects.acreate(model_id="zai/glm-4.7-flash", order=0)
        await ChatModel.objects.acreate(model_id="mistral/mistral-small-latest", order=1)
        with patch("chat.views.build_agents", side_effect=_spy):
            response = await AsyncClient().post(
                "/chat/stream",
                data=json.dumps({"message": "hi"}),
                content_type="application/json",
            )
            await _drain(response)
        return seen

    assert asyncio.run(_run())[0] == ["zai/glm-4.7-flash", "mistral/mistral-small-latest"]


@pytest.mark.django_db(transaction=True)
def test_inactive_models_are_left_out_of_the_chain():
    seen = []

    def _spy(provider_keys, model_ids=()):
        seen.append(list(model_ids))
        return (_RecordingAgent(),)

    async def _run():
        from chat.models import ChatModel

        await ChatModel.objects.all().adelete()
        await ChatModel.objects.acreate(model_id="mistral/mistral-small-latest", order=0)
        await ChatModel.objects.acreate(model_id="zai/glm-5.2", order=1, is_active=False)
        with patch("chat.views.build_agents", side_effect=_spy):
            response = await AsyncClient().post(
                "/chat/stream",
                data=json.dumps({"message": "hi"}),
                content_type="application/json",
            )
            await _drain(response)
        return seen

    assert asyncio.run(_run())[0] == ["mistral/mistral-small-latest"]


@pytest.mark.django_db
def test_restore_gauge_measures_against_the_chain_head_not_the_env_var():
    """Drag a model with a different window to the top and the gauge follows it. Uses a
    cap above both windows, so the clamp — and therefore which model it read — shows."""
    from chat.models import ChatModel, Conversation

    ChatModel.objects.all().delete()
    ChatModel.objects.create(model_id="zai/glm-4.7-flash", order=0)  # 200k window
    conversation = Conversation.objects.create(context_tokens=100)

    with override_settings(
        CHAT_MAX_CONTEXT_TOKENS=999_999, CHAT_MODEL="mistral/mistral-small-latest"
    ):
        from django.test import Client

        data = Client().get(f"/chat/conversations/{conversation.id}/").json()

    # GLM's 200k, not mistral-small-latest's 131k: the env var is no longer the head.
    assert data["usage"]["context_limit"] == 200_000


# --- The chain's admin ------------------------------------------------------


def test_chat_model_registered_in_admin():
    from chat.models import ChatModel

    assert ChatModel in django_admin.site._registry


@pytest.mark.django_db  # the form's unique check on model_id queries the table
def test_chat_model_form_rejects_an_id_litellm_cannot_route():
    from chat.admin import ChatModelAdminForm

    form = ChatModelAdminForm(
        data={"model_id": "not-a-provider/whatever", "order": 0, "is_active": "on"}
    )
    assert not form.is_valid()
    assert "cannot route" in str(form.errors["model_id"])


@pytest.mark.django_db
def test_chat_model_form_accepts_a_model_litellm_has_not_catalogued():
    """zai/glm-5.2 routes correctly but isn't in LiteLLM's model table yet. Validating
    the model *name* would reject it; validating the provider prefix doesn't."""
    from chat.admin import ChatModelAdminForm

    form = ChatModelAdminForm(data={"model_id": "zai/glm-5.2", "order": 0, "is_active": "on"})
    assert form.is_valid(), form.errors


@pytest.mark.django_db
def test_chat_model_changelist_is_drag_orderable(client):
    """The feature itself: rows carry drag handles and the field Unfold rewrites on drop."""
    from django.contrib.auth.models import User

    client.force_login(User.objects.create_superuser("admin", "a@example.com", "pw"))
    page = client.get("/admin/chat/chatmodel/")

    assert page.status_code == 200
    html = page.content.decode()
    assert 'data-ordering-field="order"' in html
    assert "drag_indicator" in html
    assert 'name="form-0-order"' in html  # the input the drag rewrites, saved by Save


@pytest.mark.django_db
def test_drag_ordered_list_keeps_its_order_input_but_hides_the_column(client):
    """The order number is hidden by CSS, not removed: the drag rewrites that input and
    Save commits it, so the field has to stay in the DOM and in the submitted form."""
    from django.contrib.auth.models import User

    client.force_login(User.objects.create_superuser("admin", "a@example.com", "pw"))
    html = client.get("/admin/chat/chatmodel/").content.decode()

    assert 'name="form-0-order"' in html  # still submitted — hiding it must not break Save
    assert 'data-ordering-field="order"' in html  # what the CSS rule keys off


@pytest.mark.django_db
def test_facts_are_grouped_by_category(client):
    """Facts have no hand-ranked order any more: get_facts hands the model all of them
    at once, so grouping related answers together is the only ordering worth having."""
    from chat.models import Fact

    Fact.objects.create(category="Personal", question="Hobbies", answer="Chess")
    Fact.objects.create(category="Compensation", question="Salary", answer="Competitive")
    Fact.objects.create(category="Personal", question="Location", answer="Munich")

    assert [f.question for f in Fact.objects.all()] == ["Salary", "Hobbies", "Location"]


@pytest.mark.django_db
def test_admin_names_each_models_place_in_the_chain(client):
    """The list says which model is live and which are backups — position alone is
    ambiguous once an inactive row sits between them."""
    from django.contrib.auth.models import User

    from chat.models import ChatModel

    ChatModel.objects.all().delete()
    ChatModel.objects.create(model_id="mistral/mistral-small-latest", order=0)
    ChatModel.objects.create(model_id="zai/glm-5.2", order=1, is_active=False)
    ChatModel.objects.create(model_id="mistral/open-mistral-nemo", order=2)

    client.force_login(User.objects.create_superuser("admin", "a@example.com", "pw"))
    html = client.get("/admin/chat/chatmodel/").content.decode()

    assert "primary" in html
    assert "fallback 1" in html  # Nemo, despite the inactive row sitting above it


@pytest.mark.django_db
def test_admin_reports_a_model_whose_provider_has_no_key(client):
    """Adding GLM with no ZAI_API_KEY is the expected first mistake: the chain would
    quietly fail past it, so the list has to say the key is missing."""
    from django.contrib.auth.models import User

    from chat.models import ChatModel, LLMCredential

    ChatModel.objects.all().delete()
    ChatModel.objects.create(model_id="zai/glm-4.7-flash", order=0)
    client.force_login(User.objects.create_superuser("admin", "a@example.com", "pw"))

    with patch.dict("os.environ", {}, clear=True):
        assert "missing" in client.get("/admin/chat/chatmodel/").content.decode()

    LLMCredential.objects.create(provider="zai", api_key="zai-key")
    assert "admin" in client.get("/admin/chat/chatmodel/").content.decode()


@pytest.mark.django_db
def test_seeded_chain_matches_the_env_vars_it_replaced(client):
    """The seed migration preserves the previous behaviour exactly, so deploying this
    doesn't silently change which model answers."""
    from chat.agent import resolve_chain
    from chat.models import ChatModel

    active = list(ChatModel.objects.filter(is_active=True).values_list("model_id", flat=True))
    assert active == resolve_chain([])  # identical to the env-var chain
    # GLM is seeded but off — it needs a key before it can answer.
    assert ChatModel.objects.filter(model_id="zai/glm-4.7-flash", is_active=False).exists()


# --- Dropdown suggestions for model ids and providers ------------------------


def test_model_id_suggestions_are_prefixed_chat_models():
    """The dropdown offers ids the chain can actually use: provider-prefixed (the
    prefix picks the API key) and chat-mode (the table also lists embedding models)."""
    from chat.admin import _model_id_suggestions

    suggestions = _model_id_suggestions()
    assert "mistral/mistral-small-latest" in suggestions
    assert all("/" in model_id for model_id in suggestions)


def test_provider_suggestions_include_llm_providers_and_github():
    from chat.admin import _provider_suggestions

    suggestions = _provider_suggestions()
    assert "mistral" in suggestions
    assert "gemini" in suggestions
    assert "github" in suggestions  # integration tokens live in the same store


@pytest.mark.django_db
def test_chat_model_form_still_accepts_an_id_the_dropdown_does_not_offer():
    """The dropdown suggests, it must not restrict: the newest models — the reason to
    add a row at all — are exactly the ones LiteLLM's table doesn't list yet."""
    from chat.admin import ChatModelAdminForm, _model_id_suggestions

    model_id = "mistral/some-model-from-the-future"
    assert model_id not in _model_id_suggestions()
    form = ChatModelAdminForm(data={"model_id": model_id, "order": 0, "is_active": "on"})
    assert form.is_valid(), form.errors


@pytest.mark.django_db
def test_admin_add_pages_render_the_searchable_dropdowns(client):
    """Both add pages carry the Select2 dropdowns with free entry (data-tags) enabled —
    catches template/widget breakage the form-level tests can't see."""
    from django.contrib.auth.models import User

    client.force_login(User.objects.create_superuser("admin", "a@example.com", "pw"))

    model_add = client.get("/admin/chat/chatmodel/add/").content.decode()
    assert "unfold-admin-autocomplete" in model_add
    assert 'data-tags="true"' in model_add
    assert 'value="mistral/mistral-small-latest"' in model_add
    assert "select2.full.js" in model_add  # the widget's Media made it onto the page

    credential_add = client.get("/admin/chat/llmcredential/add/").content.decode()
    assert 'data-tags="true"' in credential_add
    assert 'value="github"' in credential_add


@pytest.mark.django_db
def test_change_form_keeps_a_saved_id_the_catalogue_does_not_know(client):
    """A saved uncatalogued id (a brand-new model) must render selected on the change
    form — a strict choice list would silently blank it and Save would wipe the row."""
    from django.contrib.auth.models import User

    from chat.models import ChatModel

    row = ChatModel.objects.create(model_id="mistral/some-model-from-the-future", order=9)
    client.force_login(User.objects.create_superuser("admin", "a@example.com", "pw"))
    html = client.get(f"/admin/chat/chatmodel/{row.pk}/change/").content.decode()

    assert 'value="mistral/some-model-from-the-future" selected' in html


@pytest.mark.django_db
def test_add_form_does_not_preselect_a_suggestion(client):
    """The blank leading option: without it the browser shows the alphabetically first
    catalogue entry as if it were chosen, and Save would create that model."""
    from django.contrib.auth.models import User

    client.force_login(User.objects.create_superuser("admin", "a@example.com", "pw"))
    html = client.get("/admin/chat/chatmodel/add/").content.decode()

    first_option = re.search(r'name="model_id".*?<option value="([^"]*)"', html, re.DOTALL)
    assert first_option is not None
    assert first_option.group(1) == ""


# --- Follow-up suggestion chips ---------------------------------------------


def _suggestions_from(body: str) -> list | None:
    """The payload of the stream's `suggestions` frame, or None if it wasn't sent."""
    for frame in body.split("\n\n"):
        line = frame.replace("data: ", "").strip()
        if not line:
            continue
        data = json.loads(line)
        if "suggestions" in data:
            return data["suggestions"]
    return None


def test_parse_suggestions_tolerates_numbering_bullets_quotes_and_preamble():
    """Preamble ("Sure, here are…") must not become a tappable chip, and must not push a
    real question past the 3-chip cap."""
    from chat.suggestions import _parse_suggestions

    text = (
        "Sure, here are three questions:\n"
        "1. What projects has he built?\n"
        "- Is he available right now?\n"
        '• "Where is he based?"\n'
        "A fourth question that must be dropped?"
    )
    assert _parse_suggestions(text) == [
        "What projects has he built?",
        "Is he available right now?",
        "Where is he based?",
    ]


def test_suggest_followups_returns_no_chips_with_no_key():
    """Unlike the scope check there is nothing to fail open to — no key just means the
    chat runs without chips."""
    from chat import suggestions

    with patch.object(suggestions, "_suggestions_key", return_value=""):
        assert asyncio.run(suggestions.suggest_followups([], "an answer", api_key=None)) == []


def test_suggest_followups_swallows_a_writer_failure():
    """Chips are garnish — a failed writer means no chips, never a broken turn."""
    from chat import suggestions

    def _boom(_key):
        raise RuntimeError("writer down")

    with patch.object(suggestions, "build_suggestions_model", side_effect=_boom):
        result = asyncio.run(
            suggestions.suggest_followups([{"role": "user", "content": "hi"}], "hello", api_key="k")
        )
    assert result == []


def test_suggest_followups_shows_the_writer_the_exchange():
    """The writer sees the visitor's question and the streamed reply — chips written
    blind would suggest what was just answered."""
    from chat import suggestions

    captured = {}

    class _Writer:
        async def ainvoke(self, messages):
            captured["prompt"] = messages[-1].content
            return SimpleNamespace(content="What else has he built?\nIs he available?")

    async def _run():
        history = [{"role": "user", "content": "does he know django?"}]
        with patch.object(suggestions, "build_suggestions_model", return_value=_Writer()):
            return await suggestions.suggest_followups(history, "He knows Django!", api_key="k")

    assert asyncio.run(_run()) == ["What else has he built?", "Is he available?"]
    assert "does he know django?" in captured["prompt"]
    assert "He knows Django!" in captured["prompt"]


@pytest.mark.django_db(transaction=True)
def test_suggestions_frame_arrives_after_the_gauge_and_before_done():
    """Chips are the last frame before done: the gauge (and its budget save) land first,
    so a disconnect during the chip writer's call can never lose them."""

    async def _run():
        with (
            patch("chat.views.build_agents", return_value=(_UsageAgent(),)),
            patch(
                "chat.views.suggest_followups",
                new=AsyncMock(return_value=["What projects has he built?"]),
            ),
        ):
            response = await AsyncClient().post(
                "/chat/stream",
                data=json.dumps({"message": "hi"}),
                content_type="application/json",
            )
            return await _drain(response)

    body = asyncio.run(_run())
    assert _suggestions_from(body) == ["What projects has he built?"]
    assert body.index('"text"') < body.index('"usage"')
    assert body.index('"usage"') < body.index('"suggestions"')
    assert body.index('"suggestions"') < body.index('"done"')


@pytest.mark.django_db(transaction=True)
def test_no_suggestions_frame_when_the_writer_returns_nothing():
    async def _run():
        with (
            patch("chat.views.build_agents", return_value=(_RecordingAgent(),)),
            patch("chat.views.suggest_followups", new=AsyncMock(return_value=[])),
        ):
            response = await AsyncClient().post(
                "/chat/stream",
                data=json.dumps({"message": "hi"}),
                content_type="application/json",
            )
            return await _drain(response)

    body = asyncio.run(_run())
    assert _suggestions_from(body) is None
    assert '"done": true' in body


@pytest.mark.django_db(transaction=True)
def test_no_suggestions_after_a_broken_turn():
    """A turn that errored ends with the error frame — chips inviting another question
    would ring hollow next to it, even when a preamble already streamed."""
    chips = AsyncMock(return_value=["Anything else?"])

    async def _run():
        with (
            patch("chat.views.build_agents", return_value=(_StreamsThenFailsAgent(),)),
            patch("chat.views.suggest_followups", new=chips),
        ):
            response = await AsyncClient().post(
                "/chat/stream",
                data=json.dumps({"message": "hi"}),
                content_type="application/json",
            )
            return await _drain(response)

    body = asyncio.run(_run())
    assert '"error"' in body
    assert _suggestions_from(body) is None
    assert chips.await_count == 0  # never even called on a broken turn


@pytest.mark.django_db(transaction=True)
def test_no_suggestions_when_the_thread_just_spent_its_budget():
    """The next send would be refused with a 403 — chips inviting one would be a lie."""
    chips = AsyncMock(return_value=["Tell me more?"])

    async def _run():
        with (
            override_settings(CHAT_MAX_CONTEXT_TOKENS=400),  # _UsageAgent reports 450
            patch("chat.views.build_agents", return_value=(_UsageAgent(),)),
            patch("chat.views.suggest_followups", new=chips),
        ):
            response = await AsyncClient().post(
                "/chat/stream",
                data=json.dumps({"message": "hi"}),
                content_type="application/json",
            )
            return await _drain(response)

    body = asyncio.run(_run())
    assert _usage_from(body)["exhausted"] is True  # the gauge still reports the spend
    assert _suggestions_from(body) is None
    assert chips.await_count == 0


@pytest.mark.django_db(transaction=True)
def test_no_suggestions_when_every_model_returned_empty():
    """The canned "couldn't find an answer" line is not a model answer — chips inviting
    follow-ups to it would spend another call on the chain that just came back empty."""
    chips = AsyncMock(return_value=["Try again?"])

    async def _run():
        with (
            patch("chat.views.build_agents", return_value=(_EmptyAgent(), _EmptyAgent())),
            patch("chat.views.suggest_followups", new=chips),
        ):
            response = await AsyncClient().post(
                "/chat/stream",
                data=json.dumps({"message": "hi"}),
                content_type="application/json",
            )
            return await _drain(response)

    body = asyncio.run(_run())
    assert "couldn't find an answer" in body  # the graceful line still streams
    assert _suggestions_from(body) is None
    assert chips.await_count == 0


@pytest.mark.django_db(transaction=True)
def test_no_suggestions_on_a_refused_message():
    """The redirect already tells the visitor what to ask — and the whole point of
    refusing early is keeping the off-topic path to one cheap call, not two."""
    chips = AsyncMock(return_value=["What are his skills?"])

    async def _run():
        with (
            patch("chat.views.build_agents", return_value=(_RecordingAgent(),)),
            patch("chat.views.is_message_in_scope", new=AsyncMock(return_value=False)),
            patch("chat.views.suggest_followups", new=chips),
        ):
            response = await AsyncClient().post(
                "/chat/stream",
                data=json.dumps({"message": "teach me python"}),
                content_type="application/json",
            )
            return await _drain(response)

    body = asyncio.run(_run())
    assert _suggestions_from(body) is None
    assert chips.await_count == 0


@pytest.mark.django_db(transaction=True)
def test_suggestions_can_be_disabled():
    """With CHAT_SUGGESTIONS_ENABLED off the writer never runs and no frame is sent."""
    chips = AsyncMock(return_value=["Should not appear"])

    async def _run():
        with (
            override_settings(CHAT_SUGGESTIONS_ENABLED=False),
            patch("chat.views.build_agents", return_value=(_RecordingAgent(),)),
            patch("chat.views.suggest_followups", new=chips),
        ):
            response = await AsyncClient().post(
                "/chat/stream",
                data=json.dumps({"message": "hi"}),
                content_type="application/json",
            )
            return await _drain(response)

    body = asyncio.run(_run())
    assert _suggestions_from(body) is None
    assert chips.await_count == 0


# --- Message ratings (thumbs up/down) ---------------------------------------


def _message_id_from(body: str) -> int | None:
    """The id from the stream's `message_id` frame, or None if it wasn't sent."""
    for frame in body.split("\n\n"):
        line = frame.replace("data: ", "").strip()
        if not line:
            continue
        data = json.loads(line)
        if "message_id" in data:
            return data["message_id"]
    return None


@pytest.mark.django_db
def test_message_rating_defaults_to_null():
    """An unrated message is null, not 0 — 'no opinion', so it never counts as neutral
    feedback in a conversation's totals."""
    from chat.models import Conversation, Message

    conv = Conversation.objects.create()
    message = Message.objects.create(conversation=conv, role="assistant", content="hi")
    assert message.rating is None


@pytest.mark.django_db(transaction=True)
def test_rate_a_message_up_then_down_then_clear():
    """The rating is set, not accumulated: each call replaces the last, and 0 clears it."""

    async def _run():
        from chat.models import Conversation, Message

        conv = await Conversation.objects.acreate()
        msg = await Message.objects.acreate(conversation=conv, role="assistant", content="hi")
        url = f"/chat/conversations/{conv.id}/messages/{msg.id}/rating/"
        results = []
        for value in (1, -1, 0):
            res = await AsyncClient().put(
                url, data=json.dumps({"rating": value}), content_type="application/json"
            )
            await msg.arefresh_from_db()
            results.append((res.status_code, json.loads(res.content)["rating"], msg.rating))
        return results

    assert asyncio.run(_run()) == [
        (200, 1, 1),
        (200, -1, -1),
        (200, None, None),  # 0 clears to null, never stored as 0
    ]


@pytest.mark.django_db(transaction=True)
def test_rating_an_unknown_message_is_404():
    async def _run():
        from chat.models import Conversation

        conv = await Conversation.objects.acreate()
        return await AsyncClient().put(
            f"/chat/conversations/{conv.id}/messages/999999/rating/",
            data=json.dumps({"rating": 1}),
            content_type="application/json",
        )

    assert asyncio.run(_run()).status_code == 404


@pytest.mark.django_db(transaction=True)
def test_a_message_can_only_be_rated_through_its_own_conversation():
    """The conversation UUID is the capability: a real message id under someone else's
    conversation is a 404, so ids can't be rated across threads by enumeration."""

    async def _run():
        from chat.models import Conversation, Message

        owner = await Conversation.objects.acreate()
        other = await Conversation.objects.acreate()
        msg = await Message.objects.acreate(conversation=owner, role="assistant", content="hi")
        res = await AsyncClient().put(
            f"/chat/conversations/{other.id}/messages/{msg.id}/rating/",
            data=json.dumps({"rating": 1}),
            content_type="application/json",
        )
        await msg.arefresh_from_db()
        return res.status_code, msg.rating

    status, rating = asyncio.run(_run())
    assert status == 404
    assert rating is None  # the message under the owner was left untouched


@pytest.mark.django_db(transaction=True)
def test_rating_rejects_an_out_of_range_value():
    """Only -1/0/1 are ratings; anything else is a 400, not a stored nonsense value."""

    async def _run():
        from chat.models import Conversation, Message

        conv = await Conversation.objects.acreate()
        msg = await Message.objects.acreate(conversation=conv, role="assistant", content="hi")
        res = await AsyncClient().put(
            f"/chat/conversations/{conv.id}/messages/{msg.id}/rating/",
            data=json.dumps({"rating": 2}),
            content_type="application/json",
        )
        await msg.arefresh_from_db()
        return res.status_code, msg.rating

    status, rating = asyncio.run(_run())
    assert status == 400
    assert rating is None


@pytest.mark.django_db(transaction=True)
def test_restore_carries_each_message_id_and_rating():
    """The widget needs a message's id to rate it and its rating to show the current
    thumb state — both must survive a reload."""

    async def _run():
        from chat.models import Conversation, Message

        conv = await Conversation.objects.acreate()
        await Message.objects.acreate(conversation=conv, role="user", content="hi")
        await Message.objects.acreate(
            conversation=conv, role="assistant", content="hello!", rating=1
        )
        res = await AsyncClient().get(f"/chat/conversations/{conv.id}/")
        return json.loads(res.content)["messages"]

    messages = asyncio.run(_run())
    assert all("id" in m for m in messages)
    assert messages[0]["rating"] is None  # the user message, unrated
    assert messages[1]["rating"] == 1  # the rated assistant reply


@pytest.mark.django_db(transaction=True)
def test_stream_names_the_persisted_assistant_message():
    """The reply's id streams live, so the visitor can rate it without a reload — and it
    matches the actually-persisted assistant message."""

    async def _run():
        from chat.models import Message

        with patch("chat.views.build_agents", return_value=(_RecordingAgent(reply="hello"),)):
            response = await AsyncClient().post(
                "/chat/stream",
                data=json.dumps({"message": "hi"}),
                content_type="application/json",
            )
            body = await _drain(response)
        stored = await Message.objects.aget(role="assistant")
        return _message_id_from(body), stored.id

    frame_id, stored_id = asyncio.run(_run())
    assert frame_id == stored_id


@pytest.mark.django_db(transaction=True)
def test_a_broken_turn_streams_no_message_id():
    """No reply is persisted on a broken turn, so there's nothing to rate — no frame."""
    body = _post_and_drain((_FailingAgent(), _FailingAgent()))
    assert '"error"' in body
    assert _message_id_from(body) is None


@pytest.mark.django_db
def test_conversation_admin_summarizes_ratings(client):
    """The admin totals each thread's thumbs so chats can be reviewed at a glance."""
    from django.contrib.auth.models import User

    from chat.models import Conversation, Message

    conv = Conversation.objects.create()
    Message.objects.create(conversation=conv, role="assistant", content="a", rating=1)
    Message.objects.create(conversation=conv, role="assistant", content="b", rating=1)
    Message.objects.create(conversation=conv, role="assistant", content="c", rating=-1)
    Message.objects.create(conversation=conv, role="assistant", content="d")  # unrated

    client.force_login(User.objects.create_superuser("admin", "a@example.com", "pw"))
    html = client.get("/admin/chat/conversation/").content.decode()
    assert "↑ 2" in html
    assert "↓ 1" in html
