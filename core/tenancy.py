"""Working out whose page a request is about.

Every document, fact and conversation belongs to a tenant (see `core.models.Profile`), so
the public chat has to know which one it is answering as before it reads anything. That
answer comes from the request — the host it arrived on — and never from the model or from
anything the model can influence.

Order of precedence, and why:

1. **The subdomain.** `souhaib.hirees.me` means the tenant `souhaib`, full stop. The host is
   the one part of a request the caller cannot lie about without also failing ALLOWED_HOSTS,
   so it outranks anything in the body. A visitor on one tenant's page must not be able to
   ask about another by editing a JSON field.
2. **An explicit handle**, for callers with no subdomain to offer: local development, and
   the dashboard SPA previewing a page before it goes live.
3. **`FALLBACK_TENANT_HANDLE`.** The Astro portfolio at souhaibbenfarhat.github.io posts to
   the apex host and names no tenant at all, because it was written when there was only one.
   Without this step ownership would have broken that chat on the day it shipped. Set the
   setting empty once every visitor arrives through a tenant's own subdomain, and an
   unresolvable request becomes a 404 instead.

Only **published** profiles resolve. A half-finished profile mid-onboarding is not a page,
and answering questions out of one would leak a draft. Onboarding does not come through
here — it resolves its tenant from `request.user`, which needs no lookup and no publishing.
"""

from django.conf import settings

from .models import Profile


def handle_from_host(host: str) -> str:
    """The tenant handle in a hostname, or "" when there isn't one.

    Matches only a single label directly under `TENANT_BASE_DOMAIN`, so `hirees.me` and
    `www.hirees.me` give nothing (the apex serves the marketing site, not a tenant), and
    `a.b.hirees.me` gives nothing either — Cloudflare's free certificate only covers one
    level, so a deeper name could never have been served anyway.
    """
    host = (host or "").split(":")[0].strip().lower().rstrip(".")
    base = (settings.TENANT_BASE_DOMAIN or "").strip().lower()
    if not base or not host.endswith(f".{base}"):
        return ""
    label = host[: -len(base) - 1]
    if not label or "." in label:
        return ""
    # `www` is the apex under another name; the rest are platform hosts. Checking the
    # reserved list here as well as at sign-up means a name that became reserved after
    # someone claimed it stops resolving, rather than quietly keeping its page.
    from .models import RESERVED_HANDLES

    return "" if label in RESERVED_HANDLES else label


def _tenant_query(handle: str):
    """The one lookup, shared by the sync and async paths, so they can't drift.

    Returns the Profile rather than the user id, with the user joined: callers need the
    handle (to tell the assistant whose page it is standing on) as often as they need the
    owner, and select_related keeps that a single query.
    """
    return Profile.objects.filter(handle=handle, is_published=True).select_related("user")


def _candidates(request, handle: str = "") -> list[str]:
    """Handles to try, best first. Empty entries are skipped by the callers.

    When the host names a tenant, that name is the ONLY candidate — no falling back. A
    request to `nobody.hirees.me` has asked for a specific page, and if that page doesn't
    exist the honest answer is 404. Letting it fall through to the default tenant would
    serve one person's CV under another person's address, which is worse than a dead link
    and would quietly make every unclaimed subdomain a mirror of the default page.

    The fallback only applies when the host names nobody at all — the apex, the onrender
    hostname, localhost — which is the case the Astro portfolio is in.
    """
    from_host = handle_from_host(request.get_host() if request is not None else "")
    if from_host:
        return [from_host]
    return [
        (handle or "").strip().lower(),
        (settings.FALLBACK_TENANT_HANDLE or "").strip().lower(),
    ]


def resolve_tenant(request, handle: str = "") -> Profile | None:
    """The profile this request is about, or None. Sync — for the DRF views."""
    for candidate in _candidates(request, handle):
        if candidate:
            found = _tenant_query(candidate).first()
            if found is not None:
                return found
    return None


async def aresolve_tenant(request, handle: str = "") -> Profile | None:
    """The profile this request is about, or None.

    Async because `/chat/stream` is an async view: the sync ORM raises
    SynchronousOnlyOperation on that path (the same reason `chat.views` carries both
    `_chain_model_ids` and `_achain_model_ids`).
    """
    for candidate in _candidates(request, handle):
        if candidate:
            found = await _tenant_query(candidate).afirst()
            if found is not None:
                return found
    return None


def display_name_for(profile: Profile) -> str:
    """What to call this tenant in conversation. Falls back to the handle."""
    user = profile.user
    return (user.get_full_name() or "").strip() or (user.get_username() or "") or profile.handle
