"""Agent tools.

Plain functions the LangGraph agent can call. The knowledge-base tools read the
admin-managed Fact/Document models (async ORM); the GitHub tools fetch live data.
Each returns a short string the model folds into its answer.
"""

import httpx
from django.conf import settings
from langchain_core.tools import tool

from .models import Document, Fact, LLMCredential

GITHUB_API = "https://api.github.com"


async def _github_token() -> str:
    """The GitHub token. An admin-managed credential (provider="github") takes
    precedence over the GITHUB_TOKEN env var, so it can be rotated in the admin
    with no redeploy. Returns "" when neither is set (anonymous, 60 req/hour)."""
    cred = (
        await LLMCredential.objects.filter(provider="github", is_active=True)
        .order_by("id")
        .afirst()
    )
    if cred and cred.api_key:
        return cred.api_key
    return settings.GITHUB_TOKEN


def _github_headers(token: str = "", accept: str = "application/vnd.github+json") -> dict:
    headers = {"Accept": accept}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _github_error_message(exc: httpx.HTTPError, action: str) -> str:
    """A readable message for the model to relay when a GitHub call fails, instead
    of raising (which would abort the whole chat turn). 403/429 without a token is
    almost always the 60-requests/hour anonymous rate limit."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in (403, 429):
        return (
            f"GitHub's API is temporarily rate-limited, so I couldn't {action} right "
            "now. This clears up shortly — please try again in a few minutes."
        )
    return f"I couldn't {action} right now because GitHub's API didn't respond as expected."


@tool
async def get_facts(category: str = "") -> str:
    """Look up facts about Souhaib — salary expectations, availability/start date,
    location, remote preference, hobbies, and other common recruiter questions.
    Optionally filter by category (e.g. "Compensation")."""
    qs = Fact.objects.filter(is_active=True)
    if category:
        qs = qs.filter(category__icontains=category)
    facts = [f"- {fact.question}: {fact.answer}" async for fact in qs]
    return "\n".join(facts) if facts else "No facts found."


@tool
async def get_cv() -> str:
    """Read Souhaib's CV / résumé — experience, skills, and education."""
    doc = await Document.objects.filter(slug="cv", is_active=True).afirst()
    return doc.content if doc else "No CV is available yet."


@tool
async def list_github_projects() -> str:
    """List Souhaib's public GitHub repositories with description, language, and stars."""
    url = f"{GITHUB_API}/users/{settings.GITHUB_USERNAME}/repos"
    params = {"sort": "updated", "per_page": 30, "type": "owner"}
    headers = _github_headers(await _github_token())
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            repos = response.json()
    except httpx.HTTPError as exc:
        return _github_error_message(exc, "list Souhaib's GitHub projects")
    lines = [
        f"- {repo['name']} ({repo.get('language') or 'n/a'}, ★{repo['stargazers_count']}): "
        f"{repo.get('description') or 'no description'}"
        for repo in repos
        if not repo.get("fork")
    ]
    return "\n".join(lines) if lines else "No public repositories found."


@tool
async def get_repo_readme(repo: str) -> str:
    """Read the README of one of Souhaib's GitHub repositories, given the repo name."""
    url = f"{GITHUB_API}/repos/{settings.GITHUB_USERNAME}/{repo}/readme"
    headers = _github_headers(await _github_token(), accept="application/vnd.github.raw+json")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url, headers=headers)
            if response.status_code == 404:
                return f"No repository named '{repo}' was found."
            response.raise_for_status()
            text = response.text
    except httpx.HTTPError as exc:
        return _github_error_message(exc, f"read the README for '{repo}'")
    return text[:6000]  # cap size so a long README can't blow the context window
