"""OpenAPI documentation for the streaming chat endpoint.

`POST /chat/stream` is an async Server-Sent Events view — DRF can't model a streaming
response, so it isn't a DRF view and drf-spectacular won't introspect it. Instead we
describe it by hand here and inject it into the generated schema via a postprocessing
hook (wired in settings.SPECTACULAR_SETTINGS["POSTPROCESSING_HOOKS"]).

The individual SSE frames are declared as component schemas so the frontend's generated
types include them, even though OpenAPI can't express the stream-of-frames semantics
itself. Keep these in sync with chat/views.py::chat_stream.
"""

from django.conf import settings

# --- Component schemas ------------------------------------------------------

_REQUEST_SCHEMA = {
    "type": "object",
    "required": ["message"],
    "properties": {
        "message": {
            "type": "string",
            "maxLength": settings.CHAT_MAX_MESSAGE_LENGTH,
            "description": "The user's message.",
        },
        "conversation_id": {
            "type": "string",
            "format": "uuid",
            "nullable": True,
            "description": "Continue an existing thread; omit to start a new one.",
        },
    },
}

# One frame per `data:` line in the stream. The client reads a sequence of these.
_FRAME_SCHEMAS = {
    "ChatConversationIdFrame": {
        "type": "object",
        "description": "Always the first frame — the id to continue the thread.",
        "required": ["conversation_id"],
        "properties": {"conversation_id": {"type": "string", "format": "uuid"}},
    },
    "ChatTextFrame": {
        "type": "object",
        "description": "A chunk of the assistant's answer.",
        "required": ["text"],
        "properties": {"text": {"type": "string"}},
    },
    "ChatToolFrame": {
        "type": "object",
        "description": "A tool step, for the frontend's activity animations.",
        "required": ["tool", "status"],
        "properties": {
            "tool": {
                "type": "string",
                "description": "Tool name, e.g. get_facts, get_cv, list_github_projects, "
                "get_repo_readme.",
            },
            "status": {"type": "string", "enum": ["start", "end"]},
        },
    },
    "ChatErrorFrame": {
        "type": "object",
        "description": "Sent only if every model failed before any text streamed.",
        "required": ["error"],
        "properties": {"error": {"type": "string"}},
    },
    "ChatDoneFrame": {
        "type": "object",
        "description": "Always the final frame.",
        "required": ["done"],
        "properties": {"done": {"type": "boolean", "enum": [True]}},
    },
}

_COMPONENTS = {
    "ChatStreamRequest": _REQUEST_SCHEMA,
    **_FRAME_SCHEMAS,
    "ChatStreamFrame": {
        "oneOf": [{"$ref": f"#/components/schemas/{name}"} for name in _FRAME_SCHEMAS],
        "description": "One Server-Sent Events frame. Each `data:` line is one of these.",
    },
}

# --- The path operation -----------------------------------------------------

_CHAT_STREAM_PATH = {
    "post": {
        "operationId": "chat_stream_create",
        "tags": ["chat"],
        "summary": "Stream an assistant reply (Server-Sent Events)",
        "description": (
            "Runs the LangGraph agent and streams its reply as `text/event-stream`.\n\n"
            "The body is `data: <json>\\n\\n` frames: first a `ChatConversationIdFrame`, "
            "then `ChatTextFrame` tokens interleaved with `ChatToolFrame` steps, and "
            "finally a `ChatDoneFrame` (or a `ChatErrorFrame` then done). Guarded by a "
            "per-IP rate limit and a message-length cap."
        ),
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/ChatStreamRequest"}}
            },
        },
        "responses": {
            "200": {
                "description": "The SSE stream. Each frame is a `ChatStreamFrame`.",
                "content": {
                    "text/event-stream": {
                        "schema": {"$ref": "#/components/schemas/ChatStreamFrame"}
                    }
                },
            },
            "400": {"description": "Missing/too-long message, or an invalid JSON body."},
            "429": {"description": "Per-IP rate limit exceeded."},
        },
    }
}


def add_chat_stream_path(result, generator, request, public):
    """Postprocessing hook: inject the streaming endpoint and its frame schemas."""
    result.setdefault("components", {}).setdefault("schemas", {}).update(_COMPONENTS)
    result.setdefault("paths", {})["/chat/stream"] = _CHAT_STREAM_PATH
    return result
