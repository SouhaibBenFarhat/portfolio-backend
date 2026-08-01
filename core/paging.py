"""Per-operator page size for every changelist.

Django fixes the page size on the `ModelAdmin` class, so changing it means editing code and
redeploying, and Unfold's default of 100 makes every list a wall. This makes it a control on
the page instead: pick a size, and it is remembered for every table until you change it.

**Where the choice is stored, and why the session.** A cookie would work but is readable and
editable by anything running in the page; a database column would be a migration and a model
for a preference that matters for exactly as long as you are logged in. The session is
already there, already per-operator, and already cleared on sign-out.

**Why it applies to every model at once.** A page size is a property of how *you* read a
table, not of the table. Setting it per model would mean setting it eleven times.
"""

from django.contrib.admin.views.main import ChangeList

# Deliberately small at the top. 24 fills a laptop screen without scrolling and keeps the
# pagination meaningful; Unfold's 100 means most lists have exactly one page, which is why
# the pagination controls looked decorative.
DEFAULT_PAGE_SIZE = 24
PAGE_SIZES = (24, 48, 96, 192)

PAGE_SIZE_PARAM = "per_page"
SESSION_KEY = "admin_page_size"


def resolve_page_size(request) -> int:
    """The page size for this request, and remember it if the query string sets one.

    An unknown or malformed value falls back rather than raising: this arrives in a URL that
    anyone can edit or a stale bookmark can carry, and a broken page size should never be
    able to break the page.
    """
    raw = request.GET.get(PAGE_SIZE_PARAM)
    if raw is not None:
        try:
            chosen = int(raw)
        except (TypeError, ValueError):
            chosen = None
        if chosen in PAGE_SIZES:
            request.session[SESSION_KEY] = chosen
            return chosen

    stored = request.session.get(SESSION_KEY)
    return stored if stored in PAGE_SIZES else DEFAULT_PAGE_SIZE


class PageSizeChangeList(ChangeList):
    """A ChangeList whose page size comes from the request.

    Set in `get_results` rather than `__init__`: `list_per_page` is read there to build the
    paginator AND to decide `multi_page`, so changing it any later shows the right rows with
    the wrong pagination, and there is no earlier hook that has the request in hand.
    """

    def get_results(self, request):
        self.list_per_page = resolve_page_size(request)
        super().get_results(request)

    def get_filters_params(self, params=None):
        """Keep the page size out of the filter parameters.

        Everything in the query string that Django does not recognise is treated as a field
        lookup, so without this `?per_page=48` raises IncorrectLookupParameters — the admin's
        "Please correct the error below" page — instead of paginating.
        """
        lookup_params = super().get_filters_params(params)
        lookup_params.pop(PAGE_SIZE_PARAM, None)
        return lookup_params


class PageSizeAdminMixin:
    """Mix in ahead of the ModelAdmin base to get the shared page size and its control."""

    list_per_page = DEFAULT_PAGE_SIZE

    def get_changelist(self, request, **kwargs):
        return PageSizeChangeList
