"""Give the existing account a profile, and hand it everything already in the database.

Before this, nothing had an owner: the documents, facts and conversations on this instance
all belonged to the one person running it. Ownership only means something once every
existing row has one, so this migration makes tenant #1 exist and assigns them.

The handle comes from `settings.DEFAULT_TENANT_HANDLE`, not from a literal here, for the
same reason `chat/migrations/0010_seed_chat_models` reads its model ids from settings:
another instance running this code has a different first user, and a migration that
assumed ours would hand them a stranger's name.

On a fresh database — CI, a test run, a first deploy — there is no user and nothing to
assign, so every step below is a no-op and the next migration's NOT NULL still applies
cleanly to empty tables.
"""

from django.conf import settings
from django.db import migrations


def _first_account(User):
    """Which account becomes tenant #1: the earliest staff account.

    Staff rather than superuser, and rather than any account at all, because every document
    and fact on this instance was created through `/admin`, and `/admin` requires `is_staff`.
    The account that has been managing the content is the account that owns it. A visitor
    who signed in through the public flow has no staff flag and can't be the answer.

    This is a guess, and it is worth knowing it can miss: a preview or setup admin made
    early enough would have a lower id than the real owner and would inherit everything.
    An explicit setting was tried and dropped as more ceremony than the problem deserves —
    the migration runs once, on a database whose accounts you can read before deploying.
    If it does pick wrong, the fix is a reassignment, not a rollback: point `owner_id` at
    the right user on the three tables and move the Profile with it.

    Falls back to the earliest account of any kind, so an instance whose first user was
    never made staff still gets its rows assigned rather than silently orphaning them.
    """
    return (
        User.objects.filter(is_staff=True).order_by("id").first()
        or User.objects.order_by("id").first()
    )


def backfill(apps, schema_editor):
    User = apps.get_model(settings.AUTH_USER_MODEL)
    Profile = apps.get_model("core", "Profile")
    owner = _first_account(User)
    if owner is None:
        return  # fresh database: nothing to own, nobody to own it

    # is_published is True here and False for everyone after: this tenant's page is already
    # live and answering, so a migration that switched it off would take it down.
    Profile.objects.get_or_create(
        user_id=owner.pk,
        defaults={"handle": settings.DEFAULT_TENANT_HANDLE, "is_published": True},
    )

    for app_label, model_name in (("chat", "Document"), ("chat", "Fact"), ("chat", "Conversation")):
        apps.get_model(app_label, model_name).objects.filter(owner__isnull=True).update(
            owner_id=owner.pk
        )


def unbackfill(apps, schema_editor):
    # Deliberately does not delete the Profile row: reversing this migration should undo the
    # assignment, not remove an account's identity. Re-applying it is a get_or_create.
    for app_label, model_name in (("chat", "Document"), ("chat", "Fact"), ("chat", "Conversation")):
        apps.get_model(app_label, model_name).objects.update(owner_id=None)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0004_profile"),
        # The owner columns have to exist before anything can be written into them.
        ("chat", "0013_conversation_owner_document_owner_fact_owner_and_more"),
    ]

    operations = [migrations.RunPython(backfill, unbackfill)]
