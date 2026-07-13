"""Agent tools.

Plain functions the LangGraph agent can call. The knowledge-base tools read the
admin-managed Fact/Document models (async ORM); the GitHub tools fetch live data.
Each returns a short string the model folds into its answer.
"""

import httpx
from django.conf import settings
from langchain_core.tools import tool

from .models import Document, Fact

GITHUB_API = "https://api.github.com"


def _github_headers(accept: str = "application/vnd.github+json") -> dict:
    headers = {"Accept": accept}
    if settings.GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {settings.GITHUB_TOKEN}"
    return headers


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
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(url, params=params, headers=_github_headers())
        response.raise_for_status()
        repos = response.json()
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
    headers = _github_headers(accept="application/vnd.github.raw+json")
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(url, headers=headers)
        if response.status_code == 404:
            return f"No repository named '{repo}' was found."
        response.raise_for_status()
        text = response.text
    return text[:6000]  # cap size so a long README can't blow the context window
