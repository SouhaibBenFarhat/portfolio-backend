"""URL routes for the chat app."""

from django.urls import path

from . import views

app_name = "chat"

urlpatterns = [
    path("stream", views.chat_stream, name="stream"),
    path(
        "conversations/<uuid:conversation_id>/",
        views.conversation_detail,
        name="conversation_detail",
    ),
]
