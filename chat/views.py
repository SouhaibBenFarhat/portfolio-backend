"""Chat endpoints.

Phase 1: a minimal streaming chat. A single model (via LiteLLM) streams its reply
token-by-token over Server-Sent Events. No tools, no persistence yet — this proves
the async streaming pipe end to end. Those arrive in later phases.
"""

import json

import litellm
from django.conf import settings
from django.http import JsonResponse, StreamingHttpResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt


def _sse(payload: dict) -> str:
    """Format a dict as a Server-Sent Events `data:` frame."""
    return f"data: {json.dumps(payload)}\n\n"


def demo_page(request):
    """A minimal browser page to test streaming during development."""
    return render(request, "chat/demo.html")


@csrf_exempt
async def chat_stream(request):
    """Stream a model reply as Server-Sent Events.

    Expects a POST with JSON `{"message": "..."}`. Responds with a
    `text/event-stream` of `{"text": "..."}` frames, then a final `{"done": true}`.
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

    async def event_stream():
        try:
            response = await litellm.acompletion(
                model=settings.CHAT_MODEL,
                messages=[{"role": "user", "content": message}],
                stream=True,
            )
            async for chunk in response:
                token = chunk.choices[0].delta.content or ""
                if token:
                    yield _sse({"text": token})
        except Exception as exc:  # surface the failure to the client instead of hanging
            yield _sse({"error": str(exc)})
        yield _sse({"done": True})

    response = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"  # disable proxy buffering so tokens flush live
    return response
