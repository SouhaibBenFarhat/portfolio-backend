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
    "ChatModelFrame": {
        "type": "object",
        "description": "Names the model answering this turn — its LiteLLM id, e.g. "
        '"mistral/mistral-small-latest". Sent once, before the reply; the client maps it '
        "to a display name. Omitted when the provider doesn't report a model.",
        "required": ["model"],
        "properties": {"model": {"type": "string"}},
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
        "required": ["tool", "label", "status"],
        "properties": {
            "tool": {
                "type": "string",
                "description": "Raw tool name, e.g. get_facts, get_cv, list_documents, "
                "read_document, list_github_projects, get_repo_readme.",
            },
            "label": {
                "type": "string",
                "description": 'Human-readable label for the step, e.g. "reading the CV". '
                "A generic fallback is sent for a tool with no configured label.",
            },
            "status": {"type": "string", "enum": ["start", "end"]},
        },
    },
    "ChatMessageIdFrame": {
        "type": "object",
        "description": "The id of the persisted assistant reply, so the client can rate it "
        "(thumbs up/down via the rating endpoint) without waiting for a reload. Sent once, "
        "after the answer; absent when the turn broke and nothing was persisted.",
        "required": ["message_id"],
        "properties": {"message_id": {"type": "integer"}},
    },
    "ChatUsageFrame": {
        "type": "object",
        "description": "How full the thread's context is, for the client's gauge. Sent once, "
        "once the answer is complete, and omitted when the provider reported no usage.",
        "required": ["usage"],
        "properties": {"usage": {"$ref": "#/components/schemas/ChatUsage"}},
    },
    "ChatSuggestionsFrame": {
        "type": "object",
        "description": "Follow-up questions the visitor could ask next, rendered as "
        "tappable chips. Sent once, after the usage frame and just before done; omitted "
        "when no model actually answered, the thread spent its budget, or nothing usable "
        "was generated — chips simply don't appear that turn.",
        "required": ["suggestions"],
        "properties": {
            "suggestions": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 3,
                "description": "Up to 3 short questions, in the visitor's voice.",
            }
        },
    },
    "ChatErrorFrame": {
        "type": "object",
        "description": "The turn failed. Can arrive after some answer text has already "
        "streamed (a failure on the step after a tool call), so treat it as ending the "
        "turn, not as replacing what was shown.",
        "required": ["error"],
        "properties": {
            "error": {
                "type": "string",
                "description": "A friendly message safe to show any visitor.",
            },
            "detail": {
                "type": "string",
                "description": "The raw technical cause (the provider exception), for the "
                "owner to diagnose from. Show only in internal/owner mode, never to the "
                "public. May be absent.",
            },
        },
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
            "then a `ChatModelFrame` naming the answering model, then `ChatTextFrame` "
            "tokens interleaved with `ChatToolFrame` steps, then a `ChatMessageIdFrame` "
            "naming the persisted reply, a `ChatUsageFrame`, a `ChatSuggestionsFrame` with "
            "follow-up chips, and finally a `ChatDoneFrame` (or a `ChatErrorFrame` then "
            "done). Guarded by a per-IP rate limit and a message-length cap."
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
            "403": {
                "description": "The conversation spent its context budget. Start a new "
                "chat; the body carries the final `usage` figures."
            },
            "429": {"description": "Per-IP rate limit exceeded."},
        },
    }
}


def add_chat_stream_path(result, generator, request, public):
    """Postprocessing hook: inject the streaming endpoint and its frame schemas."""
    result.setdefault("components", {}).setdefault("schemas", {}).update(_COMPONENTS)
    result.setdefault("paths", {})["/chat/stream"] = _CHAT_STREAM_PATH
    return result
