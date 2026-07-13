"""URL routes for the chat app."""

from django.urls import path

from . import views

app_name = "chat"

urlpatterns = [
    path("", views.demo_page, name="demo"),
    path("stream", views.chat_stream, name="stream"),
]
