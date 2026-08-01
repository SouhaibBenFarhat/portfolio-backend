"""Make ownership mandatory, now that every existing row has an owner.

Written by hand rather than generated: `makemigrations` can only ask what default to give
the rows it assumes are still null, and there are none — `core/0005_backfill_tenant_one`
runs first and assigns every one of them. This migration is the point of that one.

Mandatory matters more than it looks. A nullable `owner` would leave two holes: a row
belonging to nobody is a document or fact the agent could serve on any tenant's page, and
the `unique_document_per_owner` constraint added in 0013 does not bind rows whose owner is
NULL — Postgres treats each NULL as distinct, so two ownerless documents could share the
slug "cv" and the constraint would not notice.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("chat", "0013_conversation_owner_document_owner_fact_owner_and_more"),
        # The backfill has to have run, or this turns every existing row into an error.
        ("core", "0005_backfill_tenant_one"),
    ]

    operations = [
        migrations.AlterField(
            model_name="conversation",
            name="owner",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="conversations",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="fact",
            name="owner",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="facts",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="document",
            name="owner",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="documents",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
