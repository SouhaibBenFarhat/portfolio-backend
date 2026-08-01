"""Renders the shared design tokens into the public pages.

The tokens live in `core/tokens.py`, which the admin also reads — see that module for why
there is one source. This tag is how the templates get them without a stylesheet: these
pages must render on a cold instance with nothing collected, so the CSS has to be inline.
"""

from django import template
from django.utils.safestring import mark_safe

from core.tokens import site_css

register = template.Library()


@register.simple_tag
def site_tokens() -> str:
    """The `:root` blocks, for use inside an existing `<style>` element."""
    # Safe by construction: every value comes from the module above, never from a request.
    return mark_safe(site_css())  # noqa: S308 — generated CSS, no user input
