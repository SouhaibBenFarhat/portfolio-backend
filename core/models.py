"""Core models.

`OAuthCredential` holds a social-login app's client id + secret, managed in the admin and
encrypted at rest. allauth normally reads these from its own `SocialApp` table (which stores
the secret in plaintext) or from settings; we keep them here instead so the secret is
Fernet-encrypted in the database — the same protection as `chat.LLMCredential` — and feed
them to allauth through a custom adapter (see `core.adapter`).
"""

from django.db import models

from chat.fields import EncryptedTextField


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
