"""Serializers for the chat API.

Used both to shape the DRF responses and to drive the OpenAPI schema (and, from it,
the frontend's generated types).
"""

from rest_framework import serializers

from .models import Message


class MessageSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=Message.Role.choices, help_text='"user" or "assistant".')
    content = serializers.CharField()


class ConversationRestoreSerializer(serializers.Serializer):
    """The stored conversation returned by the restore endpoint."""

    id = serializers.UUIDField(help_text="The conversation's UUID.")
    messages = MessageSerializer(many=True, help_text="Turns oldest-first.")
