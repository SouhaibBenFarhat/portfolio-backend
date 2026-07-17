"""Chat persistence models.

A Conversation groups an anonymous chat session (identified by an unguessable
UUID, no login). Messages store the turn-by-turn history so the assistant
remembers context across messages and across server restarts.
"""

import uuid

from django.db import models

from .fields import EncryptedTextField


class Conversation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # How many tokens the model read on this thread's last turn — the whole prompt
    # (persona + history + tool results), which already includes every earlier reply.
    # It's the thread's context size, not a running total, so each turn overwrites it.
    # Once it passes CHAT_MAX_CONTEXT_TOKENS the thread is spent and refuses new messages.
    context_tokens = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return f"Conversation {self.id}"


class Message(models.Model):
    class Role(models.TextChoices):
        USER = "user", "User"
        ASSISTANT = "assistant", "Assistant"

    conversation = models.ForeignKey(
        Conversation, related_name="messages", on_delete=models.CASCADE
    )
    role = models.CharField(max_length=16, choices=Role.choices)
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.role}: {self.content[:50]}"


class Fact(models.Model):
    """A short question/answer the assistant can cite (edited in the admin)."""

    category = models.CharField(
        max_length=50,
        help_text='Group label, e.g. "Compensation", "Availability", "Personal".',
    )
    question = models.CharField(max_length=200, help_text="e.g. Salary expectations")
    answer = models.TextField()
    is_active = models.BooleanField(default=True, help_text="Uncheck to hide without deleting.")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # Grouped by category. get_facts hands the model every fact at once, so their
        # sequence is a weak signal at best — there's nothing here worth hand-ranking,
        # and category groups related answers together better than a number would.
        ordering = ["category", "question"]

    def __str__(self):
        return f"{self.category}: {self.question}"


class Document(models.Model):
    """Long-form content (CV, bio) the assistant can read (edited in the admin).

    A document can be uploaded as a file (PDF, Word, text) in the admin: its text is
    extracted into `content` — the only form the agent's tools read — and the original
    bytes are kept in `file_data` so the admin can preview/download the file. The blob
    lives in the database, not on disk, because Render's free-tier disk is ephemeral
    (the same reason the app uses Postgres over SQLite).
    """

    slug = models.SlugField(unique=True, help_text='Stable id, e.g. "cv" or "bio".')
    title = models.CharField(max_length=200)
    content = models.TextField(
        help_text="What the assistant reads. Filled from an uploaded file's extracted "
        "text — editable afterwards, e.g. to fix a rough PDF extraction."
    )
    file_data = models.BinaryField(null=True, blank=True)  # original upload, for preview
    file_name = models.CharField(max_length=255, blank=True, editable=False)
    file_content_type = models.CharField(max_length=100, blank=True, editable=False)
    file_uploaded_at = models.DateTimeField(null=True, blank=True, editable=False)
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["slug"]

    def __str__(self):
        return self.title


class ChatModel(models.Model):
    """A model the chat can use, and its place in the failover chain.

    The rows are dragged into order in the admin: the first active one answers every
    turn, and the ones below it are tried in order when it fails. Keeping the chain in
    the database means it can be reordered, extended past two models, or have one
    disabled with no redeploy — the same reasoning as LLMCredential, which holds the
    keys these models authenticate with.

    An empty table falls back to the CHAT_MODEL/CHAT_FALLBACK_MODEL env vars (see
    chat.agent.resolve_chain): Render's free Postgres can be wiped, and an unconfigured
    table must never take the chat down.
    """

    model_id = models.CharField(
        max_length=100,
        unique=True,
        help_text="LiteLLM model id, including its provider prefix — e.g. "
        '"mistral/mistral-small-latest" or "zai/glm-4.7-flash". The prefix selects '
        "which API credential is used.",
    )
    order = models.PositiveIntegerField(
        default=0, help_text="Drag the rows to reorder, then press Save. Lowest runs first."
    )
    is_active = models.BooleanField(
        default=True, help_text="Uncheck to take a model out of the chain without deleting it."
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["order", "id"]
        verbose_name = "chat model"
        verbose_name_plural = "chat models"

    def __str__(self):
        return self.model_id

    @property
    def provider(self) -> str:
        """The LiteLLM provider prefix, which is also the LLMCredential.provider that
        supplies this model's key."""
        return self.model_id.split("/", 1)[0]


class RequestLog(models.Model):
    """One row per chat request, used for per-IP rate limiting. Rows older than the
    rate-limit window are pruned on each request, so the table stays small."""

    ip = models.CharField(max_length=45, db_index=True)  # fits IPv6
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]


class LLMCredential(models.Model):
    """An API key for an external service, managed in the admin and encrypted at rest.

    Holds both LLM provider keys (provider is the LiteLLM prefix, e.g. "mistral",
    "groq") and integration tokens (e.g. "github"). Multiple keys per provider are
    allowed and tried in order. Storing keys here means they can be added or rotated
    in the admin with no redeploy — an admin key takes precedence over the env var.
    """

    provider = models.CharField(
        max_length=50,
        help_text='Provider / integration name, e.g. "mistral", "groq", or "github".',
    )
    label = models.CharField(
        max_length=100, blank=True, help_text="Optional note, e.g. which account the key is from."
    )
    api_key = EncryptedTextField(help_text="Stored encrypted; only the last 4 chars show.")
    is_active = models.BooleanField(default=True, help_text="Uncheck to disable without deleting.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["provider", "id"]
        verbose_name = "API credential"
        verbose_name_plural = "API credentials"

    def __str__(self):
        return f"{self.provider} ({self.label or 'key'})"


class TokenUsage(models.Model):
    """Cumulative token consumption per model per calendar month.

    Summed from the usage each response reports (input + output), so it reflects real
    cost: every turn resends the whole thread, so the same history is re-billed each
    turn and consumption is the sum of every call — not the context size, which the
    Conversation.context_tokens gauge tracks instead. This is the app's own tally;
    Mistral's free tier exposes no usage API to our tier, so we count what we send and
    receive. It only sees traffic through this service, so it approximates the real
    figure (it won't match Mistral's console to the token)."""

    model = models.CharField(
        max_length=100, help_text='LiteLLM model id, e.g. "mistral/mistral-small-latest".'
    )
    period = models.DateField(help_text="First day of the calendar month this row totals.")
    input_tokens = models.PositiveBigIntegerField(default=0)
    output_tokens = models.PositiveBigIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-period", "model"]
        constraints = [
            models.UniqueConstraint(
                fields=["model", "period"], name="unique_token_usage_model_period"
            )
        ]

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __str__(self):
        return f"{self.model} {self.period:%Y-%m}: {self.total_tokens:,} tokens"
