"""The tenant record: one profile per user, carrying the handle their page is served at."""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import core.models


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("core", "0003_trust_the_registered_providers"),
    ]

    operations = [
        migrations.CreateModel(
            name="Profile",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "handle",
                    models.CharField(
                        help_text="Their public page address — the souhaib in "
                        "souhaib.hirees.me. Lowercase letters, numbers and hyphens.",
                        max_length=63,
                        unique=True,
                        validators=[core.models.validate_handle],
                    ),
                ),
                (
                    "github_username",
                    models.CharField(
                        blank=True,
                        help_text="Their GitHub username, for the assistant's project "
                        "tools. Blank if not connected.",
                        max_length=39,
                    ),
                ),
                (
                    "is_published",
                    models.BooleanField(
                        default=False,
                        help_text="Whether the public page at this handle is live. "
                        "Off while onboarding.",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "user",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="profile",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["handle"]},
        ),
    ]
