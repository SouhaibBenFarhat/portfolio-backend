"""Chat persistence models.

A Conversation groups an anonymous chat session (identified by an unguessable
UUID, no login). Messages store the turn-by-turn history so the assistant
remembers context across messages and across server restarts.
"""

import uuid

from django.db import models


class Conversation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
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
