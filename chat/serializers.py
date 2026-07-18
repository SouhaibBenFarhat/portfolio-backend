"""Serializers for the chat API.

Used both to shape the DRF responses and to drive the OpenAPI schema (and, from it,
the frontend's generated types).
"""

from rest_framework import serializers

from .models import Message


class MessageSerializer(serializers.Serializer):
    id = serializers.IntegerField(help_text="Message id — the target of the rating endpoint.")
    role = serializers.ChoiceField(choices=Message.Role.choices, help_text='"user" or "assistant".')
    content = serializers.CharField()
    rating = serializers.IntegerField(
        allow_null=True, help_text="Stored feedback: +1 up, -1 down, null if unrated."
    )


class MessageRatingRequestSerializer(serializers.Serializer):
    """The body of a rating request: the new thumbs value for one message."""

    rating = serializers.IntegerField(
        min_value=-1,
        max_value=1,
        help_text="+1 thumbs up, -1 thumbs down, 0 to clear a previous rating.",
    )


class MessageRatingSerializer(serializers.Serializer):
    """A message's rating after it's set — what the rating endpoint returns."""

    id = serializers.IntegerField(help_text="The rated message's id.")
    rating = serializers.IntegerField(
        allow_null=True, help_text="The stored value: +1, -1, or null once cleared."
    )


class ChatUsageSerializer(serializers.Serializer):
    """The context-gauge figures for a conversation."""

    context_tokens = serializers.IntegerField(
        help_text="Tokens the model read on the last turn — the thread's whole prompt "
        "(persona + history + tool results), not a running total."
    )
    context_limit = serializers.IntegerField(help_text="The thread's token budget.")
    exhausted = serializers.BooleanField(
        help_text="True once the budget is spent: further messages are refused with a 403."
    )


class ConversationRestoreSerializer(serializers.Serializer):
    """The stored conversation returned by the restore endpoint."""

    id = serializers.UUIDField(help_text="The conversation's UUID.")
    messages = MessageSerializer(many=True, help_text="Turns oldest-first.")
    usage = ChatUsageSerializer(help_text="Rebuilds the context gauge after a reload.")
