"""Serializers for the core service endpoints.

These describe the JSON shapes of `/` and `/health` so drf-spectacular can document
them and the frontend can generate matching types. They are output-only (the endpoints
take no input).
"""

from rest_framework import serializers


class HealthSerializer(serializers.Serializer):
    status = serializers.CharField(help_text='Always "ok" when the service is live.')


class MeSerializer(serializers.Serializer):
    """The current backend session, read by the dashboard SPA to gate itself."""

    authenticated = serializers.BooleanField(
        help_text="True when this browser holds a signed-in backend session."
    )
    email = serializers.CharField(help_text="The signed-in user's email, or empty when anonymous.")
    display = serializers.CharField(
        help_text="A display name for the signed-in user, or empty when anonymous."
    )


class ServiceEndpointsSerializer(serializers.Serializer):
    landing = serializers.CharField()
    health = serializers.CharField()
    me = serializers.CharField()
    docs = serializers.CharField()
    schema = serializers.CharField()
    chat_stream = serializers.CharField()
    analytics_proxy = serializers.CharField()


class ServiceDescriptorSerializer(serializers.Serializer):
    service = serializers.CharField()
    status = serializers.CharField()
    endpoints = ServiceEndpointsSerializer()
