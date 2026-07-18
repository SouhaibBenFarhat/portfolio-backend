"""Follow-up suggestions for the chat.

Once a reply has finished streaming, a second cheap model call reads the exchange and
writes what the visitor might ask next; the client renders the questions as tappable
chips. A recruiter doesn't know what the assistant can answer — the chips do the
prompting for them.

Chips are garnish, never load-bearing: a failure, timeout, or empty result means no
chips and the turn ends normally. The view skips the call entirely when the turn broke
or the thread just spent its context budget — the next send would be refused, so
inviting one would be a lie.
"""

import asyncio
import os
import re

from django.conf import settings
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_litellm import ChatLiteLLM

from .agent import _provider_of
from .guard import _content_text

SUGGESTIONS_SYSTEM_PROMPT = (
    "You write follow-up questions for the chat on Souhaib Ben Farhat's developer "
    "portfolio: things the visitor — usually a recruiter — could ask his AI assistant "
    "next. Given the conversation, reply with exactly 3 short questions (under 12 words "
    "each), one per line, nothing else — no numbering, bullets, or commentary.\n\n"
    "Write them in the visitor's voice ('What projects has he built?'), keep them about "
    "Souhaib — his experience, skills, projects, education, availability, hiring — make "
    "them natural next steps from the last reply, and never repeat something already "
    "asked or answered."
)

# The done frame waits on this call — better no chips than a stalled stream.
_TIMEOUT_SECONDS = 4
_MAX_SUGGESTIONS = 3
# How much of the thread the writer sees. The last few turns carry the topic; more only
# grows the bill for a garnish call.
_CONTEXT_MESSAGES = 6
# How much of each message it sees. The topic survives truncation, and the free tier is
# billed per input token while the useful output is 120 tokens either way.
_CONTEXT_CHARS = 500


def _suggestions_key(explicit: str | None = None) -> str:
    """The API key for the suggestions model: an explicit (admin) key, else the provider's
    env var (e.g. MISTRAL_API_KEY for a "mistral/…" model)."""
    if explicit:
        return explicit
    provider = _provider_of(settings.CHAT_SUGGESTIONS_MODEL)
    return os.getenv(f"{provider.upper()}_API_KEY", "")


def _parse_suggestions(text: str) -> list[str]:
    """Questions from the model's line-per-question reply, tolerating the numbering,
    bullets, and quotes small models add even when told not to. Only lines ending in "?"
    count — that drops preamble like "Sure, here are three questions:", which would
    otherwise become a tappable chip and push a real question past the cap."""
    suggestions = []
    for line in text.splitlines():
        cleaned = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip().strip('"')
        if cleaned.endswith("?"):
            suggestions.append(cleaned)
    return suggestions[:_MAX_SUGGESTIONS]


def build_suggestions_model(api_key: str):
    """A non-streaming model used only to write the follow-up chips. CHAT_TEMPERATURE
    (not 0) so the chips vary a little between turns instead of repeating themselves."""
    return ChatLiteLLM(
        model=settings.CHAT_SUGGESTIONS_MODEL,
        streaming=False,
        temperature=settings.CHAT_TEMPERATURE,
        max_tokens=120,
        api_key=api_key,
    )


async def suggest_followups(history: list, reply: str, api_key: str | None = None) -> list[str]:
    """Follow-up questions the visitor could ask next, as chip labels.

    `history` is the turn's model input (it already ends with the visitor's message);
    `reply` is the answer that just streamed. Returns [] — no chips, nothing else —
    when no key is configured or the call fails, times out, or writes nothing usable.
    """
    key = _suggestions_key(api_key)
    if not key:
        return []
    tail = history[-_CONTEXT_MESSAGES:]
    transcript = "\n\n".join(f"{m['role']}: {m['content'][:_CONTEXT_CHARS]}" for m in tail)
    try:
        model = build_suggestions_model(key)
        result = await asyncio.wait_for(
            model.ainvoke(
                [
                    SystemMessage(content=SUGGESTIONS_SYSTEM_PROMPT),
                    HumanMessage(content=f"{transcript}\n\nassistant: {reply[:_CONTEXT_CHARS]}"),
                ]
            ),
            timeout=_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 — chips are garnish, never break the turn
        return []
    return _parse_suggestions(_content_text(result))
