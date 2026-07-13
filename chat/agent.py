"""The chat agent, built with LangGraph.

Phase 3a: a model-only agent (no tools yet) with a system persona. The model is
routed through LiteLLM so the provider is swappable, and is injectable so tests
can pass a fake model (no API key needed). Tools arrive in Phase 3b.
"""

from functools import lru_cache

from django.conf import settings
from langchain_litellm import ChatLiteLLM
from langgraph.prebuilt import create_react_agent

SYSTEM_PROMPT = (
    "You are the AI assistant on Souhaib Ben Farhat's developer portfolio. "
    "You help recruiters and visitors learn about Souhaib — his projects, skills, "
    "and experience. Be concise, friendly, and professional. If you don't know "
    "something, say so plainly rather than inventing an answer."
)


def build_model():
    """The default chat model, routed through LiteLLM (provider set by CHAT_MODEL)."""
    return ChatLiteLLM(model=settings.CHAT_MODEL, streaming=True)


def build_agent(model=None, tools=None):
    """Build a LangGraph agent. `model` and `tools` are injectable for tests."""
    return create_react_agent(
        model or build_model(),
        tools=tools or [],
        prompt=SYSTEM_PROMPT,
    )


@lru_cache(maxsize=1)
def get_agent():
    """The production agent, compiled once and reused across requests."""
    return build_agent()
