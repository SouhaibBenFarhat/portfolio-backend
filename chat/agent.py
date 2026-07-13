"""The chat agent, built with LangGraph.

A LangGraph agent (system persona + tools) runs the chat. The model is routed
through LiteLLM, so the provider and API key are swappable. API keys come from the
admin-managed LLMCredential rows (multiple per provider, tried in order), falling
back to env vars. Agents are cached by the set of keys in use, so rotating a key
in the admin rebuilds them on the next request.
"""

from django.conf import settings
from langchain_litellm import ChatLiteLLM
from langgraph.prebuilt import create_react_agent

from .tools import get_cv, get_facts, get_repo_readme, list_github_projects

TOOLS = [get_facts, get_cv, list_github_projects, get_repo_readme]

SYSTEM_PROMPT = (
    "You are the AI assistant on Souhaib Ben Farhat's developer portfolio, helping "
    "recruiters and visitors learn about him. Use your tools instead of guessing: "
    "call get_facts for salary, availability, location, or hobbies; get_cv for "
    "experience, skills, and education; list_github_projects to show his work; and "
    "get_repo_readme to explain a specific project. Be concise, friendly, and "
    "professional. If the tools don't have an answer, say so plainly."
)

_AGENTS_CACHE: dict = {}


def build_model(model_id: str, api_key: str | None = None):
    """A chat model routed through LiteLLM. `api_key` overrides the env-var key."""
    kwargs = {"model": model_id, "streaming": True}
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


def build_agents(provider_keys: dict) -> tuple:
    """One agent per (model, key), in failover order.

    `provider_keys` maps a provider to its active keys, e.g. {"groq": ["k1", "k2"]}.
    A provider with no key uses its env var. Result is cached by the exact keys in
    use, so a change in the admin rebuilds on the next request.
    """
    model_ids = [m for m in (settings.CHAT_MODEL, settings.CHAT_FALLBACK_MODEL) if m]
    plan = [
        (model_id, key)
        for model_id in model_ids
        for key in (provider_keys.get(_provider_of(model_id)) or [None])
    ]
    signature = tuple(plan)
    if signature not in _AGENTS_CACHE:
        _AGENTS_CACHE.clear()  # keep only the current key set
        _AGENTS_CACHE[signature] = tuple(
            build_agent(model=build_model(model_id, key)) for model_id, key in plan
        )
    return _AGENTS_CACHE[signature]
