"""Turn on email linking for the providers already registered.

The field defaults to off, which is the right default for a provider nobody has looked at
yet. But the three already in use — Google, GitHub, and LinkedIn over OpenID Connect — do
verify the addresses they hand back (Google and LinkedIn assert `email_verified` in the id
token; GitHub's `user:email` scope reports a verified flag per address, and allauth only
ever matches on addresses it was told are verified). Leaving them off would keep the
existing dead end, where someone who signed up with a password is refused their own account
for clicking the wrong button.

Only rows that exist right now are touched, and only these three provider types. A row
added later starts off, so adding a provider stays a deliberate decision rather than
something a past migration decided on your behalf.
"""

from django.db import migrations

TRUSTED_PROVIDERS = ["google", "github", "openid_connect"]


def trust_registered_providers(apps, schema_editor):
    OAuthCredential = apps.get_model("core", "OAuthCredential")
    OAuthCredential.objects.filter(provider__in=TRUSTED_PROVIDERS).update(
        link_by_verified_email=True
    )


def untrust(apps, schema_editor):
    OAuthCredential = apps.get_model("core", "OAuthCredential")
    OAuthCredential.objects.filter(provider__in=TRUSTED_PROVIDERS).update(
        link_by_verified_email=False
    )


class Migration(migrations.Migration):
    dependencies = [("core", "0002_oauthcredential_link_by_verified_email")]

    operations = [migrations.RunPython(trust_registered_providers, untrust)]
