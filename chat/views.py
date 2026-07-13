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

from .agent import get_agent
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

    agent = get_agent()

    async def event_stream():
        yield _sse({"conversation_id": str(conversation.id)})
        reply_parts = []
        try:
            async for chunk, _meta in agent.astream({"messages": history}, stream_mode="messages"):
                token = getattr(chunk, "content", "") or ""
                if token:
                    reply_parts.append(token)
                    yield _sse({"text": token})
        except Exception as exc:  # surface the failure instead of hanging the stream
            yield _sse({"error": str(exc)})

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
