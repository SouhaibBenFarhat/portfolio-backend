"""Feed allauth its social apps from the encrypted `OAuthCredential` model.

allauth resolves a provider's app through `SocialAccountAdapter.list_apps()`, which blends
apps from the database and from settings — building each settings app as an in-memory
`SocialApp`. We override it to also build in-memory `SocialApp`s from our `OAuthCredential`
rows, decrypting the secret only here in memory. The credential is therefore stored
encrypted (unlike allauth's plaintext `SocialApp` table) while allauth still gets exactly
what it needs. Wired via `settings.SOCIALACCOUNT_ADAPTER`.
"""

from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.socialaccount.models import SocialApp

from .models import OAuthCredential

# Messages allauth queues immediately before we redirect the browser off this site, to the
# dashboard SPA at APP_URL (LOGIN_REDIRECT_URL). A separate application cannot render
# Django's message queue, so these are never shown — they just sit in the session until the
# visitor happens to open a page here, and then surface long after the fact, next to
# whatever that page actually says. Not queuing them is the fix; the SPA greets its own
# visitors.
_MESSAGES_NOBODY_WILL_SEE = frozenset({"account/messages/logged_in.txt"})


class AccountAdapter(DefaultAccountAdapter):
    """Drops messages that would outlive the page they were meant for.

    Wired via `settings.ACCOUNT_ADAPTER`. Everything else is allauth's default: sign-out
    still says so, and that one now lands on `/signin`, which does render it.
    """

    def add_message(self, request, level, message_template=None, *args, **kwargs):
        if message_template in _MESSAGES_NOBODY_WILL_SEE:
            return
        return super().add_message(request, level, message_template, *args, **kwargs)


def _social_app(cred: OAuthCredential) -> SocialApp:
    """An in-memory (unsaved) SocialApp built from an encrypted credential row."""
    app = SocialApp(
        provider=cred.provider,
        provider_id=cred.provider_id,
        name=cred.name or cred.get_provider_display(),
        client_id=cred.client_id,
        secret=cred.secret,  # EncryptedTextField decrypts on read; stays in memory only
    )
    # Always set the key, never leave it absent: allauth falls through to the global setting
    # only when the app doesn't answer, and this row is the answer we want it to use.
    app.settings = {"email_authentication": cred.link_by_verified_email}
    if cred.server_url:
        # OpenID Connect discovers its endpoints from the issuer URL in the app's settings.
        app.settings["server_url"] = cred.server_url
    return app


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    def list_apps(self, request, provider=None, client_id=None):
        # Start from allauth's own resolution (empty in practice — no settings APPS, no DB
        # SocialApp rows), then add ours. The filtering mirrors the base: a query for a
        # provider matches either the provider type or an OpenID Connect sub-provider id.
        apps = list(super().list_apps(request, provider=provider, client_id=client_id))
        for cred in OAuthCredential.objects.filter(is_active=True):
            if provider and provider not in (cred.provider, cred.provider_id):
                continue
            if client_id and cred.client_id != client_id:
                continue
            apps.append(_social_app(cred))
        return apps
