"""Chat endpoints.

A LangGraph agent streams its reply token-by-token over Server-Sent Events, with
conversation history persisted so the assistant remembers context. The public
endpoint is guarded by a per-IP rate limit, a message-length cap, and a bound on
how much history is sent to the model.
"""

import json
import re
import uuid
from collections import defaultdict
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError
from django.db.models import F
from django.http import JsonResponse, StreamingHttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from .agent import build_agents, context_limit, primary_model
from .guard import GUARD_BLOCK_MESSAGE, is_reply_safe
from .models import ChatModel, Conversation, LLMCredential, Message, RequestLog, TokenUsage
from .serializers import ConversationRestoreSerializer
from .tools import tool_label

# Cap the restore payload so a very long thread can't return an unbounded response.
CHAT_HISTORY_FETCH_LIMIT = 200


def _chain_model_ids() -> list[str]:
    """The admin's failover chain, in order (see ChatModel). Sync, for the DRF views."""
    return list(ChatModel.objects.filter(is_active=True).values_list("model_id", flat=True))


async def _achain_model_ids() -> list[str]:
    """The admin's failover chain, in order (see ChatModel). Async, for the stream —
    the sync ORM would raise SynchronousOnlyOperation on this path."""
    return [
        model_id
        async for model_id in ChatModel.objects.filter(is_active=True).values_list(
            "model_id", flat=True
        )
    ]


def _sse(payload: dict) -> str:
    """Format a dict as a Server-Sent Events `data:` frame."""
    return f"data: {json.dumps(payload)}\n\n"


def _tool_frame(name: str, status: str) -> str:
    """An SSE tool-step frame: the raw tool name, its human-readable label (for the
    frontend's activity animations), and whether the step is starting or ending."""
    return _sse({"tool": name, "label": tool_label(name), "status": status})


_SENTENCE_BOUNDARY = re.compile(r"[.!?](?=\s)|\n")


def _next_chunk_len(text: str, max_chars: int) -> int:
    """How many leading characters of `text` are ready to release as a guard-checked
    chunk: through the last completed sentence (punctuation then whitespace), or a hard
    cut at `max_chars` once the buffer is that long. 0 means "wait for more"."""
    boundary = None
    for match in _SENTENCE_BOUNDARY.finditer(text):
        boundary = match
    if boundary:
        return boundary.end()
    if len(text) >= max_chars:
        return max_chars
    return 0


def _token_budget(model_id: str) -> int:
    """A conversation's token ceiling: our configured cap, never above what the model
    can actually read. Clamping means a misconfigured cap can't wedge every chat."""
    window = context_limit(model_id)
    cap = settings.CHAT_MAX_CONTEXT_TOKENS
    return min(cap, window) if window else cap


def _usage_payload(context_tokens: int, model_id: str) -> dict:
    """The context-gauge figures shared by the stream and the restore endpoint."""
    budget = _token_budget(model_id)
    return {
        "context_tokens": context_tokens,
        "context_limit": budget,
        "exhausted": context_tokens >= budget,
    }


class ConversationDetailView(APIView):
    """Return a stored conversation's messages so the client can restore the thread
    after a page reload.

    Responds 404 when the id is unknown (e.g. Render's free database was reset), which
    the client treats as "start fresh". Possession of the unguessable UUID is the access
    check — the same model as the streaming endpoint. A sync DRF view (so drf-spectacular
    documents it); Django runs it in a threadpool under the async server.
    """

    @extend_schema(
        responses={
            200: ConversationRestoreSerializer,
            404: OpenApiResponse(description="Unknown or expired conversation — start fresh."),
        },
        summary="Restore a conversation",
        description="Fetch a stored conversation's messages (oldest-first, bounded to the "
        "most recent 200) to rehydrate the chat widget after a page reload.",
    )
    def get(self, request, conversation_id):
        conversation = Conversation.objects.filter(id=conversation_id).first()
        if conversation is None:
            raise NotFound("conversation not found")
        # Take the most recent messages (bounded), then restore chronological order.
        recent = list(conversation.messages.order_by("-created_at")[:CHAT_HISTORY_FETCH_LIMIT])
        recent.reverse()
        data = ConversationRestoreSerializer(
            {
                "id": conversation.id,
                "messages": recent,
                "usage": _usage_payload(
                    conversation.context_tokens, primary_model(_chain_model_ids())
                ),
            }
        ).data
        return Response(data)

    @extend_schema(
        responses={
            204: OpenApiResponse(description="Conversation deleted."),
            404: OpenApiResponse(description="Unknown conversation."),
        },
        summary="Delete a conversation",
        description="Permanently delete a conversation and all its messages. The client "
        "should also drop its stored conversation id. Possessing the UUID is the check.",
    )
    def delete(self, request, conversation_id):
        # Messages cascade-delete via the Message.conversation FK (on_delete=CASCADE).
        deleted, _ = Conversation.objects.filter(id=conversation_id).delete()
        if not deleted:
            raise NotFound("conversation not found")
        return Response(status=204)


def _text_of(content) -> str:
    """Plain text from a message's content — a string, or a list of content blocks
    like [{"type": "text", "text": "..."}] (some providers return the latter)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "") if isinstance(block, dict) else str(block) for block in content
        )
    return ""


def _client_ip(request) -> str:
    """The caller's real IP, used as the per-IP rate-limit key.

    Read Cloudflare's CF-Connecting-IP: Render fronts every service with Cloudflare,
    which sets this header to the true client IP and overwrites whatever the client
    sent — so it can't be forged. X-Forwarded-For is deliberately NOT trusted here:
    Render only appends to it and never strips the client's, so its leftmost entry is
    attacker-controlled — a spoofer could rotate it to mint unlimited rate-limit keys
    and walk past the per-IP limit. Fall back to X-Forwarded-For then REMOTE_ADDR only
    for local dev, where there's no Cloudflare in front and spoofing isn't a concern.
    """
    cloudflare_ip = request.headers.get("CF-Connecting-IP", "").strip()
    if cloudflare_ip:
        return cloudflare_ip
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "") or "unknown"


async def _is_rate_limited(ip: str) -> bool:
    """True if this IP is over its allowance. Prunes old rows and logs this hit."""
    cutoff = timezone.now() - timedelta(seconds=settings.CHAT_RATE_WINDOW_SECONDS)
    await RequestLog.objects.filter(created_at__lt=cutoff).adelete()
    recent = await RequestLog.objects.filter(ip=ip, created_at__gte=cutoff).acount()
    if recent >= settings.CHAT_RATE_LIMIT:
        return True
    await RequestLog.objects.acreate(ip=ip)
    return False


async def _record_token_usage(model_id: str, input_tokens: int, output_tokens: int):
    """Add this turn's token counts to the model's running total for the current month.

    An atomic F() increment on the (model, month) row, created on the month's first hit.
    The IntegrityError retry covers two first-writes racing to create the same row."""
    period = timezone.now().date().replace(day=1)
    updated = await TokenUsage.objects.filter(model=model_id, period=period).aupdate(
        input_tokens=F("input_tokens") + input_tokens,
        output_tokens=F("output_tokens") + output_tokens,
    )
    if updated:
        return
    try:
        await TokenUsage.objects.acreate(
            model=model_id,
            period=period,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
    except IntegrityError:
        await TokenUsage.objects.filter(model=model_id, period=period).aupdate(
            input_tokens=F("input_tokens") + input_tokens,
            output_tokens=F("output_tokens") + output_tokens,
        )


async def _get_or_create_conversation(conversation_id) -> Conversation:
    """Resume the conversation if the id is a known UUID, else start a new one."""
    if conversation_id:
        try:
            uuid.UUID(str(conversation_id))
        except (ValueError, TypeError):
            conversation_id = None
    if conversation_id:
        existing = await Conversation.objects.filter(id=conversation_id).afirst()
        if existing:
            return existing
    return await Conversation.objects.acreate()


@csrf_exempt
async def chat_stream(request):
    """Stream a model reply as Server-Sent Events, persisting the exchange.

    Expects POST JSON `{"message": "...", "conversation_id": "<optional uuid>"}`.
    The first frame is `{"conversation_id": ...}` (so the client can continue the
    thread), followed by `{"text": ...}` token frames, a `{"usage": ...}` frame with
    the turn's context-window figures, and a final `{"done": true}`.
    """
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    if await _is_rate_limited(_client_ip(request)):
        return JsonResponse({"error": "rate limit exceeded, try again later"}, status=429)

    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid JSON body"}, status=400)

    message = (payload.get("message") or "").strip()
    if not message:
        return JsonResponse({"error": "message is required"}, status=400)
    if len(message) > settings.CHAT_MAX_MESSAGE_LENGTH:
        return JsonResponse({"error": "message is too long"}, status=400)

    conversation = await _get_or_create_conversation(payload.get("conversation_id"))

    # The admin's ordered chain: its head answers, the rest are the failover order. Read
    # before the budget gate, which measures the thread against the head's context window.
    model_ids = await _achain_model_ids()
    head_model = primary_model(model_ids)

    # This thread has read its budget's worth of context; every further turn would resend
    # all of it. Stop here instead, and let the visitor start a fresh conversation.
    if conversation.context_tokens >= _token_budget(head_model):
        return JsonResponse(
            {
                "error": "this conversation has reached its context limit, start a new chat",
                "usage": _usage_payload(conversation.context_tokens, head_model),
            },
            status=403,
        )

    # Build the model's input from stored history plus the new message, bounded to the
    # most recent messages so a long thread can't blow the context window or cost.
    history = [
        {"role": msg.role, "content": msg.content} async for msg in conversation.messages.all()
    ]
    history.append({"role": "user", "content": message})
    history = history[-settings.CHAT_MAX_HISTORY_MESSAGES :]
    await Message.objects.acreate(
        conversation=conversation, role=Message.Role.USER, content=message
    )

    # API keys come from the admin (encrypted), grouped by provider and tried in
    # order; a provider with no admin key falls back to its env var.
    provider_keys: dict[str, list[str]] = {}
    async for cred in LLMCredential.objects.filter(is_active=True):
        provider_keys.setdefault(cred.provider, []).append(cred.api_key)
    agents = build_agents(provider_keys, model_ids)

    async def event_stream():
        yield _sse({"conversation_id": str(conversation.id)})
        released = ""  # answer text approved by the guard and already sent to the client
        pending = ""  # answer tokens accumulated, not yet reviewed or sent
        blocked = False  # the guard vetoed the answer → stop and show a redirect
        emitted = False  # whether any text/tool frame has been sent to the client
        error = None
        # Guard key: an admin key for the guard model's provider, else its env var.
        guard_provider = settings.CHAT_GUARD_MODEL.split("/", 1)[0]
        guard_key = (provider_keys.get(guard_provider) or [None])[0]
        # Tokens Mistral actually processed this turn, per model, summed across every
        # model call and every failover attempt — each call is billed in full, so this
        # is real consumption (unlike the gauge below, which keeps only the largest call).
        consumed: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # model -> [in, out]
        prompt_tokens = 0  # set per attempt below; bound here in case `agents` is empty
        usage_model = head_model

        async def _approved(text: str) -> bool:
            """The guard's verdict on the reply so far (True = release), or True when the
            guard is disabled."""
            if not settings.CHAT_GUARD_ENABLED:
                return True
            return await is_reply_safe(text, guard_key)

        async def _flush(force: bool = False):
            """Release guard-approved chunks of `pending` as text frames. On a veto, set
            `blocked` and stop — the rejected text is discarded, never sent."""
            nonlocal released, pending, blocked, emitted
            while pending and not blocked:
                cut = (
                    len(pending)
                    if force
                    else _next_chunk_len(pending, settings.CHAT_GUARD_MAX_CHARS)
                )
                if cut == 0:
                    return
                chunk = pending[:cut]
                if await _approved(released + chunk):
                    released += chunk
                    pending = pending[cut:]
                    emitted = True
                    yield _sse({"text": chunk})
                else:
                    blocked = True

        # Per-turn failover: try each model in order. Only fall back to the next model if
        # nothing has streamed yet — never switch models mid-answer. Regenerate on an
        # *empty* response too: the model answers on retry, or the next model does.
        for agent in agents:
            error = None
            final_text = ""  # the answer, in case it arrives as one message not streamed
            got_text = False
            tools_shown = False
            # Reset per attempt so a failed or empty attempt's gauge numbers don't leak.
            prompt_tokens = 0
            usage_model = head_model
            try:
                async for event in agent.astream_events({"messages": history}, version="v2"):
                    kind = event["event"]
                    if kind == "on_chat_model_stream":
                        token = _text_of(getattr(event["data"]["chunk"], "content", ""))
                        if token:
                            got_text = True
                            pending += token
                            async for frame in _flush():
                                yield frame
                            if blocked:
                                break
                    elif kind == "on_chat_model_end":
                        output = event["data"].get("output")
                        content = _text_of(getattr(output, "content", ""))
                        if content:
                            final_text = content
                        # A ReAct turn makes several model calls (decide a tool, then
                        # answer with its result). The largest prompt is the fullest the
                        # context got, which is what the client's gauge should show.
                        usage = getattr(output, "usage_metadata", None) or {}
                        if (usage.get("input_tokens") or 0) > prompt_tokens:
                            prompt_tokens = usage["input_tokens"]
                            # ChatLiteLLM reports the prefixed id ("mistral/…"), which is
                            # what context_limit() needs; failover may change which model
                            # answered, so read it from the event rather than assuming.
                            usage_model = (event.get("metadata") or {}).get(
                                "ls_model_name"
                            ) or usage_model
                        # Consumption: sum every call's input+output (each is billed in
                        # full), attributed to the model that actually ran this call.
                        call_input = usage.get("input_tokens") or 0
                        call_output = usage.get("output_tokens") or 0
                        if call_input or call_output:
                            call_model = (event.get("metadata") or {}).get(
                                "ls_model_name"
                            ) or head_model
                            consumed[call_model][0] += call_input
                            consumed[call_model][1] += call_output
                    elif kind == "on_tool_start":
                        tools_shown = emitted = True
                        yield _tool_frame(event["name"], "start")
                    elif kind == "on_tool_end":
                        yield _tool_frame(event["name"], "end")
                # If the answer arrived only as a final message (no streamed tokens),
                # route it through the guard too.
                if not got_text and final_text:
                    got_text = True
                    pending += final_text
                # Release the reviewed tail (a final sentence has no trailing space, so it
                # waits until the stream ends).
                if pending and not blocked:
                    async for frame in _flush(force=True):
                        yield frame
                if got_text or tools_shown:
                    break  # got an answer (or a veto), or already showed tool steps
                # Empty and nothing shown yet → loop to regenerate with the next model.
            except Exception as exc:  # noqa: BLE001 — any failure triggers failover
                error = exc
                if emitted:
                    break  # already streamed part of an answer — don't switch models

        reply = released
        if blocked:
            # The guard vetoed the answer. Any safe prefix already shown stays; add a
            # professional redirect in place of the rejected remainder.
            yield _sse({"text": GUARD_BLOCK_MESSAGE})
            reply = f"{released}\n\n{GUARD_BLOCK_MESSAGE}".strip()
        elif not reply:
            if error is not None:
                yield _sse({"error": str(error)})
            else:
                # Every retry came back empty (rare) — a graceful line so it's never blank.
                reply = (
                    "Sorry, I couldn't find an answer to that. You can ask about "
                    "Souhaib's projects, experience, skills, or availability."
                )
                yield _sse({"text": reply})

        if reply:
            await Message.objects.acreate(
                conversation=conversation, role=Message.Role.ASSISTANT, content=reply
            )

        # Persist this turn's consumption (all attempts) so the admin can total tokens
        # per model against the free-tier ceiling. Cumulative, unlike the gauge below.
        for call_model, (in_tokens, out_tokens) in consumed.items():
            await _record_token_usage(call_model, in_tokens, out_tokens)

        # Record how full the thread's context got, so the next turn can be refused once
        # it's spent and a reload can restore the gauge. Omitted when the provider
        # reported no usage, so a missing number never looks like an empty context.
        if prompt_tokens:
            conversation.context_tokens = prompt_tokens
            await conversation.asave(update_fields=["context_tokens", "updated_at"])
            yield _sse({"usage": _usage_payload(prompt_tokens, usage_model)})
        yield _sse({"done": True})

    response = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"  # disable proxy buffering so tokens flush live
    return response
