"""Chat endpoints.

A LangGraph agent streams its reply token-by-token over Server-Sent Events, with
conversation history persisted so the assistant remembers context. The public
endpoint is guarded by a per-IP rate limit, a message-length cap, and a bound on
how much history is sent to the model.
"""

import json
import uuid
from datetime import timedelta

from django.conf import settings
from django.http import JsonResponse, StreamingHttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from .agent import build_agents
from .models import Conversation, LLMCredential, Message, RequestLog

# Cap the restore payload so a very long thread can't return an unbounded response.
CHAT_HISTORY_FETCH_LIMIT = 200


def _sse(payload: dict) -> str:
    """Format a dict as a Server-Sent Events `data:` frame."""
    return f"data: {json.dumps(payload)}\n\n"


async def conversation_detail(request, conversation_id):
    """Return a stored conversation's messages so the client can restore the thread
    after a page reload.

    GET only. Responds 404 when the id is unknown (e.g. Render's free database was
    reset), which the client treats as "start fresh". Possession of the unguessable
    UUID is the access check — the same model as the streaming endpoint.
    """
    if request.method != "GET":
        return JsonResponse({"error": "GET required"}, status=405)
    conversation = await Conversation.objects.filter(id=conversation_id).afirst()
    if conversation is None:
        return JsonResponse({"error": "conversation not found"}, status=404)
    # Take the most recent messages (bounded), then restore chronological order.
    recent = [
        {"role": msg.role, "content": msg.content}
        async for msg in conversation.messages.order_by("-created_at")[:CHAT_HISTORY_FETCH_LIMIT]
    ]
    recent.reverse()
    return JsonResponse({"id": str(conversation.id), "messages": recent})


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
    """The caller's IP — the first X-Forwarded-For entry (Render sets it), else REMOTE_ADDR."""
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
    thread), followed by `{"text": ...}` token frames and a final `{"done": true}`.
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
    agents = build_agents(provider_keys)

    async def event_stream():
        yield _sse({"conversation_id": str(conversation.id)})
        reply_parts = []
        emitted = False  # whether any text/tool frame has been sent to the client
        error = None
        # Per-turn failover: try each model in order. Only fall back to the next model
        # if nothing has streamed yet — never switch models mid-answer.
        # Try each model in order. Regenerate on an *empty* response too (not just on
        # errors): the model produces a real answer on retry, or the next model does.
        for agent in agents:
            error = None
            final_text = ""  # the answer, in case it arrives as one message not streamed
            got_text = False
            tools_shown = False
            try:
                async for event in agent.astream_events({"messages": history}, version="v2"):
                    kind = event["event"]
                    if kind == "on_chat_model_stream":
                        token = _text_of(getattr(event["data"]["chunk"], "content", ""))
                        if token:
                            reply_parts.append(token)
                            got_text = emitted = True
                            yield _sse({"text": token})
                    elif kind == "on_chat_model_end":
                        content = _text_of(getattr(event["data"].get("output"), "content", ""))
                        if content:
                            final_text = content
                    elif kind == "on_tool_start":
                        tools_shown = emitted = True
                        yield _sse({"tool": event["name"], "status": "start"})
                    elif kind == "on_tool_end":
                        yield _sse({"tool": event["name"], "status": "end"})
                if not got_text and final_text:
                    reply_parts.append(final_text)
                    got_text = emitted = True
                    yield _sse({"text": final_text})
                if got_text or tools_shown:
                    break  # got an answer, or already showed tool steps (don't replay them)
                # Empty and nothing shown yet → loop to regenerate with the next model.
            except Exception as exc:  # noqa: BLE001 — any failure triggers failover
                error = exc
                if emitted:
                    break  # already streamed part of an answer — don't switch models

        reply = "".join(reply_parts)
        if not reply:
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
        yield _sse({"done": True})

    response = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"  # disable proxy buffering so tokens flush live
    return response
