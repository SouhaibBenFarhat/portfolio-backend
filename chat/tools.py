"""Agent tools.

Plain functions the LangGraph agent can call. The knowledge-base tools read the
admin-managed Fact/Document models (async ORM); the GitHub tools fetch live data.
Each returns a short string the model folds into its answer.

Every tool is READ-ONLY, and every one of them is scoped to a single tenant.

**How the tenant reaches a tool, and why it is done this way.** The owner's id travels in
the LangGraph run config (`config["configurable"]["owner_id"]`), set by the view from the
resolved request — see `core.tenancy`. LangChain injects that `config` argument and keeps it
out of the schema the model is shown, so the model cannot read the owner, cannot pass one,
and cannot ask for a different one. The alternative — binding the owner into the tools when
the agent is built — would mean one cached agent per tenant, and `chat.agent` caches by
(model, key) precisely so a 512MB instance holds one set.

A tool with no owner in its config returns "nothing found" rather than falling back to an
unscoped query. Failing closed is the whole point: the failure mode of the other choice is
one tenant's CV being read out on another tenant's page.
"""

import httpx
from django.conf import settings
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from .models import Document, Fact, LLMCredential

GITHUB_API = "https://api.github.com"

# Human-readable labels for the tools, streamed to the UI beside the raw tool name so the
# chat can show "reading the CV" instead of "get_cv". The raw name is still sent, so an
# older client that maps names itself keeps working; a tool with no entry here falls back
# to a generic label rather than leaking a function name.
TOOL_LABELS = {
    "get_facts": "loading facts",
    "get_cv": "reading the CV",
    "list_documents": "browsing documents",
    "read_document": "reading a document",
    "list_github_projects": "exploring projects",
    "get_repo_readme": "reading the project",
}


def tool_label(name: str) -> str:
    """The human-readable label for a tool name, or a generic fallback for an unmapped
    tool (so a newly added tool still streams something sensible, not its function name)."""
    return TOOL_LABELS.get(name, "working")


def _owner_id(config: RunnableConfig | None) -> int | None:
    """The tenant this run is answering as, from the run config. None when unset.

    Callers treat None as "no data", never as "no filter" — see the module docstring.
    """
    return ((config or {}).get("configurable") or {}).get("owner_id")


async def _github_username(owner_id: int | None) -> str:
    """The tenant's GitHub username, or "" when they haven't connected one.

    Reads the tenant's profile rather than settings.GITHUB_USERNAME. That setting was
    right when this instance had one tenant; using it now would list the instance owner's
    repositories on every stranger's page.
    """
    if owner_id is None:
        return ""
    from core.models import Profile

    return (
        await Profile.objects.filter(user_id=owner_id)
        .values_list("github_username", flat=True)
        .afirst()
        or ""
    )


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
async def get_facts(category: str = "", config: RunnableConfig = None) -> str:
    """Look up facts about this person — salary expectations, availability/start date,
    location, remote preference, hobbies, and other common recruiter questions.
    Optionally filter by category (e.g. "Compensation")."""
    owner_id = _owner_id(config)
    if owner_id is None:
        return "No facts found."
    qs = Fact.objects.filter(owner_id=owner_id, is_active=True)
    if category:
        qs = qs.filter(category__icontains=category)
    facts = [f"- {fact.question}: {fact.answer}" async for fact in qs]
    return "\n".join(facts) if facts else "No facts found."


def _documents(owner_id: int):
    """This tenant's active documents, with the upload blob deferred: the tools only ever
    read text, and dragging a multi-megabyte file out of Postgres on every chat turn would
    spike the single 512MB worker."""
    return Document.objects.filter(owner_id=owner_id, is_active=True).defer("file_data")


@tool
async def get_cv(config: RunnableConfig = None) -> str:
    """Read this person's CV / résumé — experience, skills, and education."""
    owner_id = _owner_id(config)
    if owner_id is None:
        return "No CV is available yet."
    doc = await _documents(owner_id).filter(slug="cv").afirst()
    return doc.content if doc else "No CV is available yet."


@tool
async def list_documents(config: RunnableConfig = None) -> str:
    """List the documents available about this person (slug and title) — e.g. their CV,
    cover letter, certificates, or anything else they have uploaded. Read one with
    read_document(slug)."""
    owner_id = _owner_id(config)
    if owner_id is None:
        return "No documents are available yet."
    docs = [f"- {doc.slug}: {doc.title}" async for doc in _documents(owner_id)]
    return "\n".join(docs) if docs else "No documents are available yet."


@tool
async def read_document(slug: str, config: RunnableConfig = None) -> str:
    """Read one of this person's documents by its slug (see list_documents) — e.g. a cover
    letter or a certificate. For their CV / experience / skills, prefer get_cv."""
    owner_id = _owner_id(config)
    if owner_id is None:
        return f"No document named '{slug}' was found."
    doc = await _documents(owner_id).filter(slug=slug).afirst()
    if not doc:
        return f"No document named '{slug}' was found."
    return doc.content[:6000]  # cap size so a long document can't blow the context window


@tool
async def list_github_projects(config: RunnableConfig = None) -> str:
    """List this person's public GitHub repositories with description, language, and stars."""
    username = await _github_username(_owner_id(config))
    if not username:
        return "They haven't connected a GitHub account yet."
    url = f"{GITHUB_API}/users/{username}/repos"
    params = {"sort": "updated", "per_page": 30, "type": "owner"}
    headers = _github_headers(await _github_token())
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            repos = response.json()
    except httpx.HTTPError as exc:
        return _github_error_message(exc, "list their GitHub projects")
    lines = [
        f"- {repo['name']} ({repo.get('language') or 'n/a'}, ★{repo['stargazers_count']}): "
        f"{repo.get('description') or 'no description'}"
        for repo in repos
        if not repo.get("fork")
    ]
    return "\n".join(lines) if lines else "No public repositories found."


@tool
async def get_repo_readme(repo: str, config: RunnableConfig = None) -> str:
    """Read the README of one of this person's GitHub repositories, given the repo name."""
    username = await _github_username(_owner_id(config))
    if not username:
        return "They haven't connected a GitHub account yet."
    url = f"{GITHUB_API}/repos/{username}/{repo}/readme"
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
