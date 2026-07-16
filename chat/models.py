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
    order = models.IntegerField(default=0, help_text="Lower numbers show first.")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["order", "category", "question"]

    def __str__(self):
        return f"{self.category}: {self.question}"


class Document(models.Model):
    """Long-form content (CV, bio) the assistant can read (edited in the admin)."""

    slug = models.SlugField(unique=True, help_text='Stable id, e.g. "cv" or "bio".')
    title = models.CharField(max_length=200)
    content = models.TextField()
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["slug"]

    def __str__(self):
        return self.title


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
