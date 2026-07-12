from django.urls import path

from .views import posthog_proxy

urlpatterns = [
    path("<path:subpath>", posthog_proxy),
]
