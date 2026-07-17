"""The chat agent, built with LangGraph.

A LangGraph agent (system persona + tools) runs the chat. The model is routed
through LiteLLM, so the provider and API key are swappable. Which models run, and in
what failover order, comes from the admin-managed ChatModel rows (falling back to the
CHAT_MODEL/CHAT_FALLBACK_MODEL env vars when there are none); API keys come from the
admin-managed LLMCredential rows (multiple per provider, tried in order), falling
back to env vars. Agents are cached by the exact plan of (model, key) pairs in use, so
reordering the chain or rotating a key in the admin rebuilds them on the next request.
"""

from collections.abc import Sequence
from functools import lru_cache

import litellm
from django.conf import settings
from langchain_litellm import ChatLiteLLM
from langgraph.prebuilt import create_react_agent

from .tools import (
    get_cv,
    get_facts,
    get_repo_readme,
    list_documents,
    list_github_projects,
    read_document,
)

TOOLS = [get_facts, get_cv, list_documents, read_document, list_github_projects, get_repo_readme]

# Kept deliberately small. An instruction-tuned model already answers naturally and follows
# a visitor's "be shorter"/"more detail" on its own — a long prompt only smothers that. So
# this sets a light default (brief, natural, visitor-led), states the single real constraint
# (topic: only Souhaib), and lists the tools it has. It does NOT enumerate how to phrase
# answers — that's the model's job, steered by the visitor. The tool line and "act this turn"
# stay because weak free models genuinely skip tools or promise to "look it up" without them.
SYSTEM_PROMPT = (
    "You are Souhaib Ben Farhat's AI assistant on his developer portfolio, helping "
    "visitors — usually recruiters — get to know him. Keep replies brief, natural, and to "
    "the point, and follow the visitor's lead on how they want you to answer.\n\n"
    "The one rule: only talk about Souhaib — his experience, skills, projects, education, "
    "availability, and hiring. If asked about something else, say that's not what you're "
    "here for and point back to what you can help with.\n\n"
    "Use your tools to get real information instead of guessing: get_facts (salary, "
    "availability, location, hobbies), get_cv (experience, skills, education), "
    "list_documents / read_document (other uploads, e.g. a cover letter), "
    "list_github_projects and get_repo_readme (his code). Call the tool this turn and then "
    "answer — never say you'll look something up and then stop. If a tool has no data, say "
    "he hasn't listed that yet."
)

_AGENTS_CACHE: dict = {}


def build_model(model_id: str, api_key: str | None = None):
    """A chat model routed through LiteLLM. `api_key` overrides the env-var key."""
    kwargs = {"model": model_id, "streaming": True, "temperature": settings.CHAT_TEMPERATURE}
    if api_key:
        kwargs["api_key"] = api_key
    return ChatLiteLLM(**kwargs)


def build_agent(model=None, tools=None):
    """Build a LangGraph agent. `model` and `tools` are injectable for tests."""
    return create_react_agent(
        model or build_model(settings.CHAT_MODEL),
        tools=TOOLS if tools is None else tools,
        prompt=SYSTEM_PROMPT,
    )


def _provider_of(model_id: str) -> str:
    return model_id.split("/", 1)[0]


@lru_cache(maxsize=8)
def context_limit(model_id: str) -> int:
    """The model's context window, in tokens. 0 when unknown.

    Looked up from LiteLLM's model table, so the number tracks the configured model
    instead of being hardcoded. The id must carry its provider prefix
    ("mistral/mistral-small-latest"); a bare name raises. Cached — it's a static table.
    """
    try:
        return litellm.get_model_info(model_id).get("max_input_tokens") or 0
    except Exception:  # noqa: BLE001 — unknown model shouldn't break a chat turn
        return 0


def resolve_chain(model_ids: Sequence[str] = ()) -> list[str]:
    """The models to try this turn, in failover order.

    `model_ids` is the admin's ordered chain (see ChatModel). An empty chain falls back
    to the env-var pair: Render's free Postgres can be wiped and a fresh deploy starts
    with no rows, neither of which may take the chat down.
    """
    chain = [model_id for model_id in model_ids if model_id]
    return chain or [m for m in (settings.CHAT_MODEL, settings.CHAT_FALLBACK_MODEL) if m]


def primary_model(model_ids: Sequence[str] = ()) -> str:
    """The model that actually answers — the head of the chain. It's what the context
    gauge measures against, since that's whose window and reply the visitor sees."""
    chain = resolve_chain(model_ids)
    return chain[0] if chain else settings.CHAT_MODEL


def build_agents(provider_keys: dict, model_ids: Sequence[str] = ()) -> tuple:
    """One agent per (model, key), in failover order.

    `provider_keys` maps a provider to its active keys, e.g. {"groq": ["k1", "k2"]}.
    `model_ids` is the admin's ordered chain; empty falls back to the env vars. A
    provider with no key uses its env var. Result is cached by the exact plan in use,
    so rotating a key *or* dragging the chain into a new order in the admin rebuilds
    on the next request.
    """
    plan = [
        (model_id, key)
        for model_id in resolve_chain(model_ids)
        for key in (provider_keys.get(_provider_of(model_id)) or [None])
    ]
    signature = tuple(plan)
    if signature not in _AGENTS_CACHE:
        _AGENTS_CACHE.clear()  # keep only the current key set
        _AGENTS_CACHE[signature] = tuple(
            build_agent(model=build_model(model_id, key)) for model_id, key in plan
        )
    return _AGENTS_CACHE[signature]
