"""Feed allauth its social apps from the encrypted `OAuthCredential` model.

allauth resolves a provider's app through `SocialAccountAdapter.list_apps()`, which blends
apps from the database and from settings — building each settings app as an in-memory
`SocialApp`. We override it to also build in-memory `SocialApp`s from our `OAuthCredential`
rows, decrypting the secret only here in memory. The credential is therefore stored
encrypted (unlike allauth's plaintext `SocialApp` table) while allauth still gets exactly
what it needs. Wired via `settings.SOCIALACCOUNT_ADAPTER`.
"""

import logging

from allauth.account.adapter import DefaultAccountAdapter
from allauth.core import context
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.socialaccount.models import SocialApp
from django.contrib import messages

from .models import OAuthCredential

logger = logging.getLogger(__name__)

# Shown when transactional mail can't go out. Deliberately vague about the cause — a visitor
# can do nothing with an SMTP error — but honest that nothing is coming, so they are not left
# refreshing an inbox for a code that will never arrive.
MAIL_FAILED_MESSAGE = "We couldn't send that email just now. Please try again in a few minutes."

# Messages allauth queues immediately before we redirect the browser off this site, to the
# dashboard SPA at APP_URL (LOGIN_REDIRECT_URL). A separate application cannot render
# Django's message queue, so these are never shown — they just sit in the session until the
# visitor happens to open a page here, and then surface long after the fact, next to
# whatever that page actually says. Not queuing them is the fix; the SPA greets its own
# visitors.
_MESSAGES_NOBODY_WILL_SEE = frozenset({"account/messages/logged_in.txt"})


class AccountAdapter(DefaultAccountAdapter):
    """Wired via `settings.ACCOUNT_ADAPTER`."""

    def add_message(self, request, level, message_template=None, *args, **kwargs):
        """Drop messages that would outlive the page they were meant for.

        Everything else is allauth's default: sign-out still says so, and that one lands on
        `/signin`, which does render it.
        """
        if message_template in _MESSAGES_NOBODY_WILL_SEE:
            return
        return super().add_message(request, level, message_template, *args, **kwargs)

    def send_mail(self, template_prefix, email, ctx):
        """Keep a mail outage from taking the whole auth flow down.

        allauth sends transactional mail *inline during the request* — the verification
        code, the password-reset link, the "account already exists" notice. Every one of
        those sends was unguarded, so a broken SMTP server did not degrade the flow, it
        raised straight out of the view: **seven** paths answered `Server Error (500)`,
        including both OAuth callbacks, sign-up, and password reset. Nothing was logged
        either, so the 500 was the only evidence it had happened.

        Failing soft is right here. The mail is a step in a flow, not the flow itself, and
        the visitor can retry — but they have to be told, or they sit refreshing an inbox
        for a code that is never coming.
        """
        try:
            super().send_mail(template_prefix, email, ctx)
        except Exception:
            # Broad on purpose: SMTP fails in many shapes (auth, DNS, TLS, timeout) and none
            # of them should cost a visitor their sign-in. The traceback goes to the log,
            # which is the only place anyone will find out.
            logger.exception("Could not send %s to %s", template_prefix, email)
            request = getattr(context, "request", None)
            if request is not None:
                messages.error(request, MAIL_FAILED_MESSAGE)


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
