"""Seed the failover chain from the env vars it replaces.

The chain was exactly CHAT_MODEL then CHAT_FALLBACK_MODEL. Seeding those two rows means
the admin list shows what is actually running the moment this deploys, instead of sitting
empty while the env-var fallback quietly drives the chat — an empty list that still
answers is a confusing thing to hand someone.

The ids are read from settings rather than hardcoded: an instance that overrode CHAT_MODEL
would otherwise be seeded with the code's defaults and silently switch models on deploy,
because the seeded rows outrank the env vars they were meant to preserve.

GLM is seeded alongside them but inactive — it needs a ZAI_API_KEY (or an admin credential
with provider "zai") before it can answer, and the admin's key column says which. Tick it
active once the key is in. Reversing removes only the rows this seeded.
"""

from django.conf import settings
from django.db import migrations

# Z.ai's free tier: $0 in and out, a 200k window, and real tool-calling support — so it
# can serve the agent's tools rather than just chat. GLM-5.2 is the stronger model but is
# paid, so it stays a deliberate choice to add rather than something a migration hands you.
GLM_MODEL_ID = "zai/glm-4.7-flash"


def seed(apps, schema_editor):
    ChatModel = apps.get_model("chat", "ChatModel")
    chain = [m for m in (settings.CHAT_MODEL, settings.CHAT_FALLBACK_MODEL) if m]
    for order, model_id in enumerate(chain):
        ChatModel.objects.get_or_create(
            model_id=model_id, defaults={"order": order, "is_active": True}
        )
    ChatModel.objects.get_or_create(
        model_id=GLM_MODEL_ID, defaults={"order": len(chain), "is_active": False}
    )


def unseed(apps, schema_editor):
    ChatModel = apps.get_model("chat", "ChatModel")
    seeded = [settings.CHAT_MODEL, settings.CHAT_FALLBACK_MODEL, GLM_MODEL_ID]
    ChatModel.objects.filter(model_id__in=[m for m in seeded if m]).delete()


class Migration(migrations.Migration):
    dependencies = [("chat", "0009_chatmodel")]

    operations = [migrations.RunPython(seed, unseed)]
