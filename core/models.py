"""Core models.

`Profile` is the tenant: one row per signed-in user, carrying the handle their public page
is served at. Everything a user owns (documents, facts, the conversations recruiters have
with their page) hangs off the user this profile belongs to.

`OAuthCredential` holds a social-login app's client id + secret, managed in the admin and
encrypted at rest. allauth normally reads these from its own `SocialApp` table (which stores
the secret in plaintext) or from settings; we keep them here instead so the secret is
Fernet-encrypted in the database — the same protection as `chat.LLMCredential` — and feed
them to allauth through a custom adapter (see `core.adapter`).
"""

import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from chat.fields import EncryptedTextField

# Handles nobody may claim, because each one is (or will be) a real host on hirees.me:
# `www` and the apex serve this app, `app` is the dashboard SPA, `mail`/`img.mail`/`r.mail`
# are Brevo's branded sending subdomains, and the rest are names we want to keep free for
# obvious platform uses. Recorded in docs/infrastructure.md alongside the wildcard-DNS plan;
# `api` is here even though api.hirees.me was removed, because the name should stay ours.
RESERVED_HANDLES = frozenset(
    {
        "admin",
        "api",
        "app",
        "assets",
        "billing",
        "blog",
        "cdn",
        "dashboard",
        "docs",
        "help",
        "img",
        "mail",
        "me",
        "r",
        "root",
        "signin",
        "signup",
        "static",
        "status",
        "support",
        "www",
    }
)

# A handle becomes a DNS label (souhaib.hirees.me), so it has to be a legal one: lowercase
# letters, digits and hyphens, starting and ending alphanumeric, 63 characters at most.
# Deliberately NOT a SlugField — slugs allow underscores and uppercase, and neither is valid
# in a hostname, so a handle that saved fine would produce a subdomain that cannot resolve.
_HANDLE_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def validate_handle(value: str) -> None:
    """Reject handles that can't be a hostname, or that belong to the platform."""
    if not _HANDLE_RE.match(value or ""):
        raise ValidationError(
            "Use lowercase letters, numbers and hyphens only, starting and ending with a "
            "letter or number.",
            code="invalid_handle",
        )
    if value in RESERVED_HANDLES:
        raise ValidationError("That name is reserved. Please choose another.", code="reserved")


class Profile(models.Model):
    """A tenant — one per user, holding the handle their public page is served at.

    Split from `User` rather than swapped in as a custom user model: `AUTH_USER_MODEL` can
    only be changed on an empty database, and this one already has accounts, allauth rows
    and social links pointing at the stock user. A 1:1 profile carries the same information
    with a migration instead of a rebuild.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile"
    )
    handle = models.CharField(
        max_length=63,  # the DNS label limit
        unique=True,
        validators=[validate_handle],
        help_text="Their public page address — the souhaib in souhaib.hirees.me. "
        "Lowercase letters, numbers and hyphens.",
    )
    # Whose repositories the agent's GitHub tools read. Per tenant, not the GITHUB_USERNAME
    # setting: that setting was correct when there was one tenant, and reading it now would
    # show this instance owner's repositories on every stranger's page. Blank means the
    # tenant hasn't connected GitHub, and the tools say so instead of guessing.
    github_username = models.CharField(
        max_length=39,  # GitHub's own limit
        blank=True,
        help_text="Their GitHub username, for the assistant's project "
        "tools. Blank if not connected.",
    )
    # Off until the owner says otherwise: onboarding fills a profile in over several turns,
    # and a half-answered page indexed by a search engine is worse than no page at all.
    is_published = models.BooleanField(
        default=False,
        help_text="Whether the public page at this handle is live. Off while onboarding.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["handle"]

    def __str__(self):
        return self.handle

    def clean(self):
        # Normalise before validating, so "Souhaib" is saved as "souhaib" rather than
        # rejected — a handle is a hostname, and hostnames are case-insensitive.
        if self.handle:
            self.handle = self.handle.strip().lower()
        super().clean()


class OAuthCredential(models.Model):
    """One social-login app (Google / GitHub / LinkedIn), with its secret encrypted at rest.

    One row per provider — and per `provider_id` for OpenID Connect, which is how a single
    provider type (openid_connect) hosts several services (LinkedIn, and others later).
    """

    class Provider(models.TextChoices):
        GOOGLE = "google", "Google"
        GITHUB = "github", "GitHub"
        OPENID_CONNECT = "openid_connect", "OpenID Connect (e.g. LinkedIn)"

    provider = models.CharField(max_length=40, choices=Provider.choices)
    provider_id = models.CharField(
        max_length=40,
        blank=True,
        help_text='OpenID Connect only, e.g. "linkedin". Blank for Google/GitHub.',
    )
    name = models.CharField(max_length=100, blank=True, help_text='Display name, e.g. "LinkedIn".')
    client_id = models.CharField(max_length=255, help_text="The OAuth app's client id (public).")
    secret = EncryptedTextField(
        help_text="Client secret. Stored encrypted; only the last 4 chars show."
    )
    server_url = models.CharField(
        max_length=255,
        blank=True,
        help_text="OpenID Connect issuer, e.g. https://www.linkedin.com/oauth.",
    )
    # Whether a sign-in through this provider may land on an account that already exists
    # under the same address. Off by default: allauth's own default is off, because a
    # provider that hands back an address it never checked could otherwise be used to walk
    # into anyone's account. Per row rather than in settings, deliberately — allauth reads
    # `app.settings["email_authentication"]` before any global value, and every OpenID
    # Connect service shares one provider id ("openid_connect"), so a settings-level flag
    # could not say "trust LinkedIn" without also trusting the next OIDC provider added.
    link_by_verified_email = models.BooleanField(
        default=False,
        verbose_name="link by verified email",
        help_text="Sign the visitor into an existing account when this provider reports "
        "their email as verified. Only tick it for providers that really verify — "
        "otherwise this is a way into any account. Off means they are told the account "
        "already exists instead.",
    )
    is_active = models.BooleanField(default=True, help_text="Uncheck to disable without deleting.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "social login app"
        verbose_name_plural = "social login apps"
        ordering = ["provider", "provider_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "provider_id"], name="unique_oauth_provider"
            )
        ]

    def __str__(self):
        return self.name or self.get_provider_display()
