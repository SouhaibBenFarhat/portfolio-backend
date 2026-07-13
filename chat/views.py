"""Chat endpoints.

A LangGraph agent streams its reply token-by-token over Server-Sent Events, with
conversation history loaded from and saved to the database so the assistant
remembers context across messages. Tools and provider failover arrive in later
phases.
"""

import json
import uuid

from django.http import JsonResponse, StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt

from .agent import get_agents
from .models import Conversation, Message


def _sse(payload: dict) -> str:
    """Format a dict as a Server-Sent Events `data:` frame."""
    return f"data: {json.dumps(payload)}\n\n"


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

    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid JSON body"}, status=400)

    message = (payload.get("message") or "").strip()
    if not message:
        return JsonResponse({"error": "message is required"}, status=400)

    conversation = await _get_or_create_conversation(payload.get("conversation_id"))

    # Build the model's input from stored history plus the new message.
    history = [
        {"role": msg.role, "content": msg.content} async for msg in conversation.messages.all()
    ]
    history.append({"role": "user", "content": message})
    await Message.objects.acreate(
        conversation=conversation, role=Message.Role.USER, content=message
    )

    agents = get_agents()

    async def event_stream():
        yield _sse({"conversation_id": str(conversation.id)})
        reply_parts = []
        emitted = False  # whether any text/tool frame has been sent to the client
        error = None
        # Per-turn failover: try each model in order. Only fall back to the next model
        # if nothing has streamed yet — never switch models mid-answer.
        for agent in agents:
            error = None
            try:
                async for event in agent.astream_events({"messages": history}, version="v2"):
                    kind = event["event"]
                    if kind == "on_chat_model_stream":
                        token = getattr(event["data"]["chunk"], "content", "") or ""
                        if token:
                            reply_parts.append(token)
                            emitted = True
                            yield _sse({"text": token})
                    elif kind == "on_tool_start":
                        emitted = True
                        yield _sse({"tool": event["name"], "status": "start"})
                    elif kind == "on_tool_end":
                        yield _sse({"tool": event["name"], "status": "end"})
                break  # this model succeeded
            except Exception as exc:  # noqa: BLE001 — any failure triggers failover
                error = exc
                if emitted:
                    break  # already streamed part of an answer — don't switch models
        if error is not None and not emitted:
            yield _sse({"error": str(error)})

        reply = "".join(reply_parts)
        if reply:
            await Message.objects.acreate(
                conversation=conversation, role=Message.Role.ASSISTANT, content=reply
            )
        yield _sse({"done": True})

    response = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"  # disable proxy buffering so tokens flush live
    return response
