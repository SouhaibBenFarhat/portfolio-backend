"""The chat agent, built with LangGraph.

Phase 3a: a model-only agent (no tools yet) with a system persona. The model is
routed through LiteLLM so the provider is swappable, and is injectable so tests
can pass a fake model (no API key needed). Tools arrive in Phase 3b.
"""

from functools import lru_cache

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


def build_model(model_id: str):
    """A chat model routed through LiteLLM for the given model id."""
    return ChatLiteLLM(model=model_id, streaming=True)


def build_agent(model=None, tools=None):
    """Build a LangGraph agent. `model` and `tools` are injectable for tests."""
    return create_react_agent(
        model or build_model(settings.CHAT_MODEL),
        tools=TOOLS if tools is None else tools,
        prompt=SYSTEM_PROMPT,
    )


@lru_cache(maxsize=1)
def get_agents():
    """The production agents, one per model, tried in order (primary then fallback).

    Compiled once and reused across requests.
    """
    model_ids = [m for m in (settings.CHAT_MODEL, settings.CHAT_FALLBACK_MODEL) if m]
    return tuple(build_agent(model=build_model(model_id)) for model_id in model_ids)
