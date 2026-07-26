"""The social sign-in providers offered across the auth surface.

One source of truth for the provider list and which of them are actually configured, shared
by `core.views` (the sign-in hub + landing hero) and the `{% social_buttons %}` template tag,
so a provider is defined once. All three are always *shown*; an unconfigured one routes to a
friendly notice instead of erroring (see `core.views.signin`). LinkedIn rides on the generic
OpenID Connect provider (its classic OAuth2 API is dead), so its route is parametrised.
"""

from allauth.socialaccount.adapter import get_adapter
from django.urls import reverse

PROVIDERS = [
    {"id": "google", "name": "Google", "url_name": "google_login", "url_kwargs": {}},
    {"id": "github", "name": "GitHub", "url_name": "github_login", "url_kwargs": {}},
    {
        "id": "linkedin",
        "name": "LinkedIn",
        "url_name": "openid_connect_login",
        "url_kwargs": {"provider_id": "linkedin"},
    },
]


def configured_provider_ids(request) -> set:
    """Provider ids with an active credential — one pass through allauth's (encrypted)
    adapter, so it agrees exactly with what happens on submit."""
    present = set()
    for app in get_adapter(request).list_apps(request):
        present.add(app.provider)
        if app.provider_id:
            present.add(app.provider_id)
    return {p["id"] for p in PROVIDERS if p["id"] in present}


def provider_buttons(request) -> list[dict]:
    """The providers ready for a template: id, name, the login URL, and whether it's
    configured (a configured button posts to allauth; an unconfigured one shows a notice)."""
    configured = configured_provider_ids(request)
    return [
        {
            "id": p["id"],
            "name": p["name"],
            "login_url": reverse(p["url_name"], kwargs=p["url_kwargs"]),
            "configured": p["id"] in configured,
        }
        for p in PROVIDERS
    ]
