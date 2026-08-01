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


@register.inclusion_tag("unfold/helpers/page_size.html", takes_context=True)
def page_size(context):
    """The rows-per-page control, for the changelist toolbar.

    Rendered as plain links rather than a <select>: a select needs JavaScript to act on a
    change, and every option here is a URL the browser can already reach on its own.

    Each link is built with `cl.get_query_string`, so the current search, filters and
    ordering survive the switch — losing your filters because you asked for more rows would
    make the control worse than useless. It drops the page number deliberately: page 7 of a
    24-row list is not page 7 of a 96-row one.
    """
    from core.paging import PAGE_SIZE_PARAM, PAGE_SIZES

    cl = context.get("cl")
    if cl is None:
        return {"options": []}

    options = [
        {
            "size": size,
            "url": cl.get_query_string({PAGE_SIZE_PARAM: size}, ["p"]),
            "current": size == cl.list_per_page,
        }
        for size in PAGE_SIZES
    ]
    return {"options": options, "current": cl.list_per_page}


@register.inclusion_tag("unfold/helpers/page_links.html", takes_context=True)
def page_links(context):
    """Page numbers, elided around the current page.

    Unfold prints every page, so 210 rows at 24 a page put nine numbers in the toolbar and a
    long list would put fifty. Django's paginator already knows how to collapse that —
    `get_elided_page_range` gives the first, the last, a window either side of where you are,
    and an ellipsis for the gaps — so this is a rendering change, not new arithmetic.
    """
    from django.core.paginator import Paginator

    cl = context.get("cl")
    if cl is None or not cl.paginator.count:
        return {"pages": [], "cl": None}

    current = cl.page_num
    pages = []
    for entry in cl.paginator.get_elided_page_range(current, on_each_side=1, on_ends=1):
        if entry == Paginator.ELLIPSIS:
            pages.append({"ellipsis": True})
        else:
            pages.append(
                {
                    "number": entry,
                    "url": cl.get_query_string({"p": entry}),
                    "current": entry == current,
                }
            )
    return {"pages": pages, "cl": cl, "multi_page": cl.paginator.num_pages > 1}


@register.simple_tag(takes_context=True)
def showing_range(context) -> str:
    """ "Showing 25 to 48 of 150 documents" — which slice of the whole is on screen.

    A bare total ("150 documents") above a page of 24 reads as though all 150 are there, and
    a bare page count doesn't say where in the list you are. The range says both.

    The two ends are computed rather than read off a Page object: `ChangeList` keeps the
    sliced `result_list` but never exposes the page itself, and `show_all` returns everything
    on page 1, which would make any arithmetic on `page_num` wrong.
    """
    cl = context.get("cl")
    if cl is None:
        return ""

    total = cl.result_count
    plural = cl.opts.verbose_name_plural
    if not total:
        return f"No {plural}"

    shown = len(cl.result_list)
    name = cl.opts.verbose_name if total == 1 else plural
    # Always the range, even when one page holds everything: "Showing all 150" and
    # "Showing 1 to 150 of 150" are the same fact, but only the second keeps its shape as
    # you change the page size, so the line never changes form under you.
    start = (cl.page_num - 1) * cl.list_per_page + 1
    return f"Showing {start} to {start + shown - 1} of {total} {name}"
