"""Template helpers for the auth surface.

`{% social_buttons %}` renders the Google/GitHub/LinkedIn buttons on any auth page (the
sign-in hub, allauth's login and signup, …) from one place, so the markup and the
"configured vs. shows-a-notice" logic live in a single template + a single helper rather
than being copied per page. The provider list + configured check are shared with
`core.views` via `core.providers`.
"""

from django import template

from core.providers import provider_buttons

register = template.Library()


@register.inclusion_tag("partials/_social_buttons.html", takes_context=True)
def social_buttons(context):
    """The social provider buttons for the current request (empty-safe if no request)."""
    request = context.get("request")
    return {"social_providers": provider_buttons(request) if request else []}
