"""Serializers for the core service endpoints.

These describe the JSON shapes of `/` and `/health` so drf-spectacular can document
them and the frontend can generate matching types. They are output-only (the endpoints
take no input).
"""

from rest_framework import serializers


class HealthSerializer(serializers.Serializer):
    status = serializers.CharField(help_text='Always "ok" when the service is live.')


class ServiceEndpointsSerializer(serializers.Serializer):
    health = serializers.CharField()
    docs = serializers.CharField()
    schema = serializers.CharField()
    chat_stream = serializers.CharField()
    analytics_proxy = serializers.CharField()


class ServiceDescriptorSerializer(serializers.Serializer):
    service = serializers.CharField()
    status = serializers.CharField()
    endpoints = ServiceEndpointsSerializer()
