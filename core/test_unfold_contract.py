"""What has to stay true about django-unfold for this admin to keep looking like itself.

The theme is not a setting. It is nine forked templates and roughly a thousand lines of CSS
that reach into names django-unfold owns: template paths, template tags, element ids, utility
class names, CSS custom properties. None of that is a public API. An upgrade can rename any
of it, and the failure is usually silent — the page still returns 200, it just stops being
styled, or a fork stops being included and you quietly get stock Unfold back.

So `django-unfold` is pinned to an exact version in requirements.txt, and these tests are the
checklist that runs when a human deliberately moves the pin. Red here does not mean the code
is broken; it means an assumption the theme rests on has changed, and someone has to look.

The tests deliberately do NOT assert on strings our own templates emit. Doing that only proves
we typed what we typed: after Unfold renames something, our fork keeps emitting the old name
and the test stays green while the admin breaks. Everything below is either derived from a
file (so it maintains itself) or is a fact about the installed package.
"""

import hashlib
import pathlib
import re

import pytest
import unfold
from django.conf import settings
from django.test import Client

UNFOLD_PACKAGE = pathlib.Path(unfold.__file__).parent
OVERRIDES = pathlib.Path(settings.BASE_DIR) / "core/static/core/unfold-overrides.css"


def _operator(username: str):
    """A logged-in staff client. The admin renders differently for anyone else."""
    from django.contrib.auth import get_user_model

    staff = get_user_model().objects.create_superuser(
        username, f"{username}@example.com", "Zephyr-Vault-92"
    )  # noqa: S106 — test-only
    client = Client()
    client.force_login(staff)
    return client


@pytest.mark.django_db
def test_every_admin_page_an_operator_opens_still_renders():
    """The cheapest test here and the one that catches the most.

    A missing template path, a template tag that no longer exists, a renamed context variable,
    a changed component signature — all of them raise on render, and all of them land on one
    of these pages. Nothing in the suite requested most of these URLs before, so an upgrade
    that broke the changelist would have been found by opening the admin, not by CI.
    """
    client = _operator("renders")
    me = client.session["_auth_user_id"]

    pages = ["/admin/", "/admin/auth/user/add/", f"/admin/auth/user/{me}/change/"]
    for group in settings.UNFOLD["SIDEBAR"]["navigation"]:
        pages.append(str(group["link"]))  # the app index each rail icon opens
        pages.extend(str(item["link"]) for item in group["items"])

    broken = {}
    for path in pages:
        response = client.get(path)
        if response.status_code != 200:
            broken[path] = response.status_code

    # The two unauthenticated pages, which use a different layout and so a different set of
    # Unfold includes. Signed out for the login page, since a signed-in visitor is redirected
    # away from it, and last for logout because it ends the session.
    if Client().get("/admin/login/").status_code != 200:
        broken["/admin/login/"] = "did not render for a signed-out visitor"
    if client.post("/admin/logout/").status_code != 200:
        broken["/admin/logout/"] = "did not render (it is a POST since Django 5)"

    assert not broken, f"admin pages no longer render: {broken}"


def test_unfolds_stylesheet_is_still_layered_and_ours_is_not():
    """The single fact the whole theme rests on, and the one nobody wrote down.

    Unfold ships its bundle wrapped in `@layer`. An unlayered declaration beats every layered
    one regardless of specificity or source order — that, not specificity and not load order,
    is why `unfold-overrides.css` wins. It is also luck: we never chose it.

    If a future Unfold ships its bundle unlayered, our low-specificity rules (`body, #main`,
    `.pf-item`, `#result_list tbody td`) start losing ties they have always won, and the admin
    degrades at HTTP 200 with nothing in any log. If we ever wrap our own sheet in a layer, we
    lose the same fight from the other side.
    """
    bundle = (UNFOLD_PACKAGE / "static/unfold/css/styles.css").read_text()

    assert "@layer" in bundle, (
        "Unfold's stylesheet is no longer wrapped in cascade layers. Our overrides beat it "
        "BECAUSE they are unlayered and its rules are layered. Re-check the whole admin: "
        "rules that used to win may now lose on specificity instead."
    )
    ours = OVERRIDES.read_text()
    assert "@layer" not in ours, (
        "unfold-overrides.css now declares a cascade layer. That forfeits the advantage the "
        "sheet was built on — layered rules lose to Unfold's unlayered ones and, for "
        "!important, to its layered ones too. Remove the @layer."
    )


# Files inside django-unfold we either copied or built markup assumptions on. The hash is not
# the point; forcing someone to read the diff is. Each entry says what we would lose.
UNFOLD_FILES_WE_DEPEND_ON = {
    "templates/admin/change_list.html": (
        "7bb5cf96a99d6a0a6f043a896e503a55c148458a4ccab606160126665da9015d",
        "forked verbatim; upstream changes to the changelist page are silently dropped",
    ),
    "templates/admin/change_list_results.html": (
        "1a2a072727ae72f9f6306a7f27c0df2dd73ea42408353a0eae462db377d83d76",
        "forked verbatim; our row markup drifts away from Unfold's stylesheet",
    ),
    "templates/unfold/helpers/search.html": (
        "c894c481b050129a08de701ba4a0c2b91266550c1080ef7a34d9fcd93dfc5f1d",
        "we style it through .pf-search > div > div, so its nesting depth is load-bearing",
    ),
    "templates/unfold/helpers/navigation_user.html": (
        "52b3821da81f42e9511776fdc1e3b8d1ba90925eae2ce3c726a2c6bed0207183",
        "the account chip styles hang off its exact nesting and its x-on:click div",
    ),
    "templates/unfold/helpers/avatar.html": (
        "2720e7b38290cfd20a52936462cf343729a82acc43c0ffb211d3edfa5aa38499",
        "we resize it by matching the h-[38px] class it hard-codes",
    ),
}

# Names we invented under unfold/helpers/. If Unfold ever ships a file at one of these paths,
# ours shadows it and the real one never loads — a collision that fails completely silently.
NAMES_WE_SQUATTED = ("page_links.html", "page_size.html")


def test_the_unfold_files_we_copied_from_have_not_changed():
    """Fork drift, and the only test that can see it.

    A fork keeps rendering after an upgrade. It renders the OLD page — missing whatever Unfold
    added, and styled for a stylesheet that has moved on. Nothing errors, so only comparing
    against the package copy finds it.
    """
    drifted, gone = [], []
    for rel, (expected, why) in UNFOLD_FILES_WE_DEPEND_ON.items():
        path = UNFOLD_PACKAGE / rel
        if not path.exists():
            gone.append(rel)
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            drifted.append(f"{rel}\n      why we care: {why}\n      now: {actual}")

    assert not gone, f"templates we fork no longer exist upstream: {gone}"
    assert not drifted, (
        "django-unfold changed files this theme is built on. Read each diff, port what "
        "matters into our copy, then update the hash here:\n    " + "\n    ".join(drifted)
    )

    collisions = [
        n for n in NAMES_WE_SQUATTED if (UNFOLD_PACKAGE / "templates/unfold/helpers" / n).exists()
    ]
    assert not collisions, (
        f"Unfold now ships {collisions}, which our templates of the same name shadow. "
        "Rename ours, or its version will never load and nothing will say so."
    )


@pytest.mark.django_db
def test_every_pf_class_the_stylesheet_styles_is_actually_rendered():
    """Catches a fork that has stopped being included — the quietest failure of the lot.

    Our navigation, pagination and results templates only run because Unfold includes those
    exact paths. If an upgrade includes something else instead, the path still exists, our
    file is still on disk, every file-existence check still passes — and the admin silently
    reverts to stock Unfold.

    The list is scraped from the stylesheet, so it costs nothing to maintain and cannot drift
    out of date: add a `.pf-` rule and this test starts requiring it to appear.
    """
    client = _operator("markers")
    html = "".join(
        [
            client.get("/admin/").content.decode(),
            client.get("/admin/auth/user/").content.decode(),
            # Signed out: a signed-in visitor is redirected away from the login page.
            Client().get("/admin/login/").content.decode(),
            # Last — this ends the session. A POST because Django 5 refuses logout by GET.
            client.post("/admin/logout/").content.decode(),
        ]
    )

    rendered = set()
    for attr in re.finditer(r'class="([^"]*)"', html):
        rendered.update(attr.group(1).split())

    styled = set(re.findall(r"\.(pf-[a-z0-9-]+)", OVERRIDES.read_text()))
    missing = sorted(styled - rendered)

    assert not missing, (
        f"the stylesheet styles {missing}, but nothing on the page carries those classes. "
        "Either the template that emits them is no longer being included by Unfold, or the "
        "rules are dead and should be deleted."
    )


def test_unfolds_defaults_have_not_moved():
    """Guards the settings we never set — the ones an upgrade can switch on underneath us.

    Every UNFOLD key we do not configure falls back to Unfold's own default. Flip one on
    upstream and new, unstyled UI appears inside our theme: a history button, a site dropdown,
    an environment banner. No name changes, so nothing else here would notice.

    BORDER_RADIUS is checked separately because it is the opposite case — a key Unfold does
    NOT define today. Our CSS reads `var(--border-radius, 6px)` and gets the 6px fallback. If
    Unfold starts emitting that variable, every radius in the admin changes at once.
    """
    from unfold.settings import CONFIG_DEFAULTS

    known = {
        "ACCOUNT", "COLORS", "COMMAND", "DASHBOARD_CALLBACK", "ENVIRONMENT",
        "ENVIRONMENT_TITLE_PREFIX", "EXTENSIONS", "FORMS", "GLOBAL_CALLBACK", "LANGUAGES",
        "LANGUAGE_FLAGS", "LOGIN", "SCRIPTS", "SHOW_BACK_BUTTON", "SHOW_HISTORY",
        "SHOW_LANGUAGES", "SHOW_UI_WARNINGS", "SHOW_VIEW_ON_SITE", "SIDEBAR", "SITE_DROPDOWN",
        "SITE_FAVICONS", "SITE_HEADER", "SITE_ICON", "SITE_LOGO", "SITE_SUBHEADER",
        "SITE_SYMBOL", "SITE_TITLE", "SITE_URL", "SITE_VIEWS", "STYLES", "TABS",
    }  # fmt: skip
    appeared = set(CONFIG_DEFAULTS) - known
    assert not appeared, (
        f"django-unfold added settings we have never seen: {sorted(appeared)}. Read what they "
        "default to — a new feature defaulting to on renders unstyled UI inside our theme."
    )

    assert "BORDER_RADIUS" not in CONFIG_DEFAULTS, (
        "Unfold now defines BORDER_RADIUS. Our CSS reads var(--border-radius, 6px) and has "
        "always taken the fallback, so every corner in the admin is about to change at once."
    )
