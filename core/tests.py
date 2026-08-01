import os
import re
from pathlib import Path
from unittest import mock

import pytest
import yaml
from django.conf import settings
from django.core import mail
from django.test import Client
from django.urls import reverse

# Rendering the landing and sign-in pages resolves configured social providers through the
# adapter (a DB query), so DB access is enabled for this whole module.
pytestmark = pytest.mark.django_db


def test_health_ok():
    response = Client().get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_accepts_head_requests():
    """Uptime monitors (UptimeRobot) default to HEAD; a GET-only view answers 405,
    which reads as the service being down even though it's healthy."""
    response = Client().head("/health")
    assert response.status_code == 200


def test_index_describes_service():
    """The descriptor moved to /api/ when the landing page took the root path."""
    response = Client().get("/api/")
    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "portfolio-backend"
    assert "/ingest/<path>" in body["endpoints"]["analytics_proxy"]


def test_root_serves_the_landing_page():
    response = Client().get("/")
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/html")
    body = response.content.decode()
    assert "hirees" in body
    # The page must stand alone: no {% static %} dependency, so it renders on a cold
    # instance with nothing collected.
    assert "{% static" not in body


def test_landing_page_has_absolute_link_preview_urls():
    """Open Graph requires absolute URLs — a relative og:image renders no card at all."""
    body = Client().get("/", secure=True).content.decode()
    assert 'property="og:title"' in body
    assert 'name="twitter:card" content="summary_large_image"' in body
    # Both must be absolute, and built from the request so they follow the host.
    for prop in ("og:url", "og:image"):
        line = next(ln for ln in body.splitlines() if f'property="{prop}"' in ln)
        assert 'content="https://' in line, f"{prop} is not absolute: {line.strip()}"
    assert "og.png" in body  # a raster card: platforms will not render an SVG


def test_landing_hero_shows_google_button_and_more_options_link():
    """The hero carries the one-click Google button and a quiet link to the full /signin
    page for the other providers."""
    body = Client().get("/").content.decode()
    assert "Continue with Google" in body
    assert 'href="/signin"' in body  # the "more options" fallback


def test_landing_hero_google_routes_to_notice_when_unconfigured():
    """With no Google credential, the hero button submits to /signin?unavailable=Google
    rather than to allauth — a click shows a friendly notice, never a 500."""
    body = Client().get("/").content.decode()
    assert 'name="unavailable" value="Google"' in body
    assert "/accounts/google/login/" not in body


def test_landing_hero_google_signs_in_when_configured():
    """Add a Google credential and the hero button becomes a real login form."""
    from core.models import OAuthCredential

    OAuthCredential.objects.create(provider="google", client_id="c", secret="s")
    body = Client().get("/").content.decode()
    assert 'action="/accounts/google/login/?process=login"' in body


def _stroke_width(svg: str) -> float:
    """The weight the mark is drawn at, read off the rendered SVG's one stroked group."""
    return float(re.search(r'stroke-width="([\d.]+)"', svg).group(1))


def _letter_paths(svg: str) -> list[str]:
    """The `d` of every <path> in the mark — the letter itself, without its full stop."""
    return re.findall(r'<path d="([^"]+)"', svg)


def _full_stop(svg: str) -> tuple[float, float, float]:
    """The mark's dot as (cx, cy, r), compared as numbers because one source writes 18.0
    and the other writes 18 for the same position."""
    circle = re.search(r'<circle cx="([\d.]+)" cy="([\d.]+)" r="([\d.]+)"', svg)
    return tuple(float(group) for group in circle.groups())


def test_the_tab_mark_is_the_same_letter_carrying_more_weight():
    """A monogram needs no redrawing at 16px, only more weight.

    The mark this replaced — a CV with a brain badged into it — needed two genuinely
    different drawings, because its three text lines merged into the badge at tab size. So
    the old assertion here was that /favicon.svg had *fewer* paths than /favicon-full.svg.
    A letter has no such problem, so both routes now draw the same thing and the only
    difference is optical size: heavier strokes and a larger dot for the tab, the way a
    type family cuts a separate face for small text instead of shrinking the display one.
    """
    small = Client().get("/favicon.svg").content.decode()
    full = Client().get("/favicon-full.svg").content.decode()

    assert small.count("<path") == full.count("<path")  # one drawing, not two
    assert _stroke_width(small) > _stroke_width(full)
    assert "linearGradient" not in small  # no gradients anywhere in this design system
    assert "#1f6f78" in small  # the site's petrol accent


def test_the_template_mark_and_the_generated_favicon_are_the_same_drawing():
    """The logo has two sources, and nothing but this test stops them parting.

    Every page that shows the wordmark includes templates/partials/_logo.html; the favicon
    routes are drawn in Python by core.views._mark. They can't be one source — a favicon is
    served standalone, so it needs literal colours instead of CSS variables, its own
    <svg xmlns>, and a heavier second weight for the browser tab.

    The mark this replaced had *four* hand-kept copies and no check at all, which is how the
    social-signup page kept getting missed. Two is the floor; this keeps it honest.
    """
    from django.template.loader import render_to_string

    from core import views

    partial = render_to_string("partials/_logo.html")

    assert _letter_paths(partial) == _letter_paths(views._LETTER)
    # The template carries the display weight, so it's FAVICON_SVG the dot must match.
    assert _full_stop(partial) == _full_stop(views.FAVICON_SVG)


def test_every_page_with_the_wordmark_uses_the_shared_mark():
    """Pages showing the wordmark must draw the shared mark, not a hand-copy.

    The landing sits outside site_base.html deliberately — it must render on a cold
    instance with nothing collected — so it includes the mark itself and is checked by
    output. socialaccount/signup used to be outside the shell too, purely as leftover
    order-of-work, and was the page that kept drifting; it now extends the shared layout,
    so it inherits the mark rather than carrying one, and that inheritance is what's
    asserted here.
    """
    from django.template.loader import get_template, render_to_string

    letter = _letter_paths(render_to_string("partials/_logo.html"))
    assert letter, "the partial itself draws nothing"

    for path in ("/", "/signin"):
        body = Client().get(path).content.decode()
        for stroke in letter:
            assert stroke in body, f"{path} does not draw the shared mark"

    source = get_template("socialaccount/signup.html").template.source
    assert '{% extends "allauth/layouts/base.html" %}' in source
    assert "<svg" not in source, "it should inherit the mark, not draw its own"


def test_favicon_is_served_as_svg():
    response = Client().get("/favicon.ico")
    assert response.status_code == 200
    assert response["Content-Type"] == "image/svg+xml"
    assert b"<svg" in response.content


# --- Logging --------------------------------------------------------------
# Django's own defaults emit nothing in production: the console handler carries
# `require_debug_true` and `mail_admins` no-ops on an empty ADMINS. An unhandled 500 wrote
# nothing anywhere, which is how a failing SMTP send inside an OAuth callback showed up as a
# bare "Server Error (500)" with no trace of the cause.


def test_errors_are_logged_whatever_debug_is_set_to():
    """The handler must not be gated on DEBUG — production is exactly where it's needed."""
    import logging

    handlers = settings.LOGGING["handlers"]
    assert handlers["console"]["class"] == "logging.StreamHandler"
    assert "filters" not in handlers["console"], "a DEBUG filter would make this a no-op in prod"
    assert "mail_admins" not in handlers, "ADMINS is empty, so that handler discards silently"

    django_logger = logging.getLogger("django")
    assert django_logger.propagate is False, "must not fall back to Django's own handlers"
    assert any(isinstance(h, logging.StreamHandler) for h in django_logger.handlers)


def test_a_view_error_is_written_out_with_its_traceback():
    """django.request is where Django reports a 500, and the traceback is the whole point —
    the previous behaviour left only the browser's error page as evidence.

    Writes through the configured handler into a buffer of our own rather than reading
    stderr: the handler binds its stream once at startup, which under pytest is the runner's
    capture object, so reading the real file descriptor proves nothing either way.
    """
    import io
    import logging

    handler = next(
        h for h in logging.getLogger("django").handlers if isinstance(h, logging.StreamHandler)
    )
    buffer = io.StringIO()
    original = handler.setStream(buffer)
    try:
        # Raised for real: an exception that was never raised carries no traceback, so
        # logging would print only its repr — which is not what Django reports on a 500.
        try:
            raise RuntimeError("smtp is down")
        except RuntimeError:
            logging.getLogger("django.request").error(
                "Internal Server Error: /accounts/google/login/callback/", exc_info=True
            )
    finally:
        handler.setStream(original)

    written = buffer.getvalue()
    assert "Internal Server Error: /accounts/google/login/callback/" in written
    assert "RuntimeError: smtp is down" in written  # the cause, not just that something broke
    assert "Traceback" in written


# --- Error pages ----------------------------------------------------------
# Django's default handlers pick these up from the root of TEMPLATES DIRS — no view, no URL
# entry, and every error from here on gets the site's design instead of a bare
# "<h1>Server Error (500)</h1>". Only visible with DEBUG=False.


@pytest.mark.parametrize(
    ("view_name", "status", "heading"),
    [
        ("bad_request", 400, "Bad request"),
        ("permission_denied", 403, "Not allowed"),
        ("page_not_found", 404, "Page not found"),
        ("server_error", 500, "Something went wrong"),
    ],
)
def test_every_error_page_renders_in_the_site_design(view_name, status, heading):
    """Each handler finds our template and draws the shared shell, not Django's bare page."""
    from django.core.exceptions import PermissionDenied, SuspiciousOperation
    from django.http import Http404
    from django.test import RequestFactory
    from django.views import defaults

    view = getattr(defaults, view_name)
    request = RequestFactory().get("/whatever")
    exceptions = {
        "bad_request": SuspiciousOperation(),
        "permission_denied": PermissionDenied(),
        "page_not_found": Http404(),
    }
    response = (
        view(request) if view_name == "server_error" else view(request, exceptions[view_name])
    )

    body = response.content.decode()
    assert response.status_code == status
    assert heading in body
    assert "hirees" in body  # the shared shell, not Django's default page
    assert "{%" not in body and "{#" not in body  # no template syntax leaked


def test_the_500_page_renders_with_no_context_at_all():
    """The property that matters. django.views.defaults.server_error calls template.render()
    with no request and no context processors, so nothing here may depend on {{ user }},
    {{ messages }} or a database — a 500 is most likely being served precisely when those are
    the broken things."""
    from django.template.loader import get_template

    body = get_template("500.html").render()  # no context, exactly as Django does it
    assert "Something went wrong" in body
    assert "{%" not in body and "{#" not in body


def test_a_missing_page_serves_the_themed_404():
    """End to end through the real handler, which only engages with DEBUG off."""
    from django.test import override_settings

    with override_settings(DEBUG=False):
        response = Client().get("/no-such-page-exists")

    assert response.status_code == 404
    assert "Page not found" in response.content.decode()


# --- OpenAPI schema + Swagger UI ------------------------------------------


def test_openapi_schema_is_served():
    response = Client().get("/api/schema/")
    assert response.status_code == 200
    body = response.content.decode()
    assert body.startswith("openapi:")
    assert "portfolio-backend API" in body


def test_swagger_ui_is_served():
    response = Client().get("/api/docs/")
    assert response.status_code == 200
    assert b"swagger-ui" in response.content.lower()


def test_schema_documents_all_public_endpoints():
    """The schema covers the JSON endpoints (DRF) and the injected streaming endpoint."""
    spec = yaml.safe_load(Client().get("/api/schema/").content)
    paths = spec["paths"]
    assert "/health" in paths
    assert "/chat/conversations/{conversation_id}/" in paths
    assert (
        "/chat/conversations/{conversation_id}/messages/{message_id}/rating/" in paths
    )  # the DRF rating endpoint
    assert "/chat/stream" in paths  # injected by the postprocessing hook
    assert "/ingest/{subpath}" not in paths  # proxy stays out of the docs

    schemas = spec["components"]["schemas"]
    assert "ChatStreamRequest" in schemas
    assert "ChatStreamFrame" in schemas  # the SSE-frame union the frontend types from
    assert "ChatSuggestionsFrame" in schemas  # the follow-up chips joined the union
    assert "ChatMessageIdFrame" in schemas  # the rateable-reply id joined the union


# --- Auth / social login (allauth) ----------------------------------------


@pytest.mark.django_db
def test_headless_config_endpoint_is_wired():
    """The dashboard SPA reads its sign-in options from allauth's headless config endpoint,
    so it must be mounted and expose the account + socialaccount blocks."""
    response = Client().get("/_allauth/browser/v1/config")
    assert response.status_code == 200
    data = response.json()["data"]
    assert "account" in data
    assert "socialaccount" in data


def test_provider_callback_urls_resolve():
    """The registered OAuth apps redirect back to these paths, so they must exist. LinkedIn
    is the trap: its OpenID Connect callback is /accounts/oidc/linkedin/…, never the dead
    linkedin_oauth2 path (that provider still calls LinkedIn's removed /v2/me endpoint)."""
    assert reverse("google_callback") == "/accounts/google/login/callback/"
    assert reverse("github_callback") == "/accounts/github/login/callback/"
    assert (
        reverse("openid_connect_callback", kwargs={"provider_id": "linkedin"})
        == "/accounts/oidc/linkedin/login/callback/"
    )


@pytest.mark.django_db
def test_oauth_credential_secret_is_encrypted_at_rest():
    """The client secret is Fernet-encrypted in the database — the raw column never holds the
    plaintext — yet reads back decrypted. Same protection as the LLM keys, and the reason we
    manage these ourselves instead of allauth's plaintext SocialApp table."""
    from django.db import connection

    from core.models import OAuthCredential

    cred = OAuthCredential.objects.create(provider="google", client_id="cid", secret="s3cr3t-value")
    with connection.cursor() as cur:
        cur.execute("SELECT secret FROM core_oauthcredential WHERE id = %s", [cred.id])
        raw = cur.fetchone()[0]
    assert raw != "s3cr3t-value"  # ciphertext sits in the column
    assert OAuthCredential.objects.get(id=cred.id).secret == "s3cr3t-value"  # decrypts on read


@pytest.mark.django_db
def test_adapter_serves_apps_from_encrypted_credentials():
    """The custom adapter turns active credential rows into the SocialApps allauth resolves,
    decrypting the secret only in memory — including LinkedIn via OpenID Connect."""
    from allauth.socialaccount.adapter import get_adapter

    from core.models import OAuthCredential

    OAuthCredential.objects.create(
        provider="openid_connect",
        provider_id="linkedin",
        name="LinkedIn",
        client_id="li-id",
        secret="li-secret",
        server_url="https://www.linkedin.com/oauth",
    )
    app = get_adapter().get_app(None, provider="linkedin")
    assert app.client_id == "li-id"
    assert app.secret == "li-secret"
    assert app.settings["server_url"] == "https://www.linkedin.com/oauth"


@pytest.mark.django_db
def test_adapter_ignores_inactive_credentials():
    """An unticked row is off the chain — no app, so its provider simply isn't offered."""
    from allauth.socialaccount.adapter import get_adapter

    from core.models import OAuthCredential

    OAuthCredential.objects.create(
        provider="github", client_id="gid", secret="gsec", is_active=False
    )
    assert get_adapter().list_apps(None, provider="github") == []


# --- Linking a social login to an existing account ------------------------
# Someone who signed up with email+password and later clicks "Continue with Google" is the
# same person. Without this they were told "Account Already Exists" and left on a dead end —
# and because that notice is an email, a broken mail server turned the whole callback into a
# 500. Whether a provider may do this is per credential row: allauth reads the app's
# `email_authentication` setting before any global one, which is the only way to say "trust
# LinkedIn" without also trusting the next OpenID Connect provider added (they share the
# provider id "openid_connect").


@pytest.mark.django_db
def test_credential_carries_its_linking_decision_to_allauth():
    """The row's flag reaches allauth as the app's `email_authentication` setting, which is
    what it consults before falling back to any global value."""
    from allauth.socialaccount.adapter import get_adapter

    from core.models import OAuthCredential

    OAuthCredential.objects.create(
        provider="google", client_id="g-id", secret="g-sec", link_by_verified_email=True
    )
    assert get_adapter().get_app(None, provider="google").settings["email_authentication"] is True


@pytest.mark.django_db
def test_linking_is_off_until_a_provider_is_explicitly_trusted():
    """The default is off, and it is sent as an explicit False rather than left absent — an
    absent key would let allauth fall through to a global setting this row never opted into."""
    from allauth.socialaccount.adapter import get_adapter

    from core.models import OAuthCredential

    cred = OAuthCredential.objects.create(provider="github", client_id="gh-id", secret="gh-sec")
    assert cred.link_by_verified_email is False
    assert get_adapter().get_app(None, provider="github").settings["email_authentication"] is False


def _google_signin(client, *, email, email_verified=True):
    """Drive a real Google sign-in, stubbing only Google itself.

    The token exchange and the id-token check are the two places that talk to Google; the
    rest is our own callback path, which is what these tests are about.
    """
    from urllib.parse import parse_qs, urlparse

    claims = {
        "iss": "https://accounts.google.com",
        "aud": "g-id",
        "sub": "google-uid-1",
        "email": email,
        "email_verified": email_verified,
        "name": "Ada Lovelace",
    }
    start = client.post("/accounts/google/login/?process=login")
    state = parse_qs(urlparse(start["Location"]).query)["state"][0]
    with (
        mock.patch(
            "allauth.socialaccount.providers.oauth2.client.OAuth2Client.get_access_token",
            return_value={"access_token": "tok", "id_token": "id.tok", "expires_in": 3599},
        ),
        mock.patch(
            "allauth.socialaccount.providers.google.views._verify_and_decode",
            return_value=claims,
        ),
    ):
        return client.get("/accounts/google/login/callback/", {"state": state, "code": "auth-code"})


@pytest.mark.django_db
def test_a_trusted_providers_verified_email_signs_into_the_existing_account():
    """The whole point: a password account and a Google login on the same verified address
    are one person, so the visitor is signed in rather than told the account already exists."""
    from django.contrib.auth import get_user_model

    from core.models import OAuthCredential

    OAuthCredential.objects.create(
        provider="google", client_id="g-id", secret="g-sec", link_by_verified_email=True
    )
    user = _verified_user("ada@example.com")

    client = Client()
    response = _google_signin(client, email="ada@example.com")

    assert response.status_code == 302
    assert response["Location"] == settings.APP_URL
    assert client.session["_auth_user_id"] == str(user.pk)
    assert get_user_model().objects.count() == 1, "must reuse the account, not make a second"


@pytest.mark.django_db
def test_linking_writes_the_connection_so_later_sign_ins_do_not_re_match_the_email():
    """AUTO_CONNECT stores the SocialAccount. Without it allauth signs them in but records
    nothing, so it re-matches on the address every time and sign-in dies the day they
    change it."""
    from allauth.socialaccount.models import SocialAccount

    from core.models import OAuthCredential

    OAuthCredential.objects.create(
        provider="google", client_id="g-id", secret="g-sec", link_by_verified_email=True
    )
    user = _verified_user("ada@example.com")

    _google_signin(Client(), email="ada@example.com")

    assert SocialAccount.objects.filter(user=user, provider="google").exists()


@pytest.mark.django_db
def test_a_password_survives_linking_so_both_routes_keep_working():
    """allauth wipes the password when it links onto an *unverified* address (that account
    may be a squatter's). Ours are verified by code at sign-up, so the password stays and
    the visitor can still sign in either way."""
    from core.models import OAuthCredential

    OAuthCredential.objects.create(
        provider="google", client_id="g-id", secret="g-sec", link_by_verified_email=True
    )
    user = _verified_user("ada@example.com")

    _google_signin(Client(), email="ada@example.com")

    user.refresh_from_db()
    assert user.has_usable_password()
    assert Client().login(username=user.username, password=_STRONG_PASSWORD)


@pytest.mark.django_db
def test_an_unverified_address_never_links_even_from_a_trusted_provider():
    """Trust lives in the provider's *verified* flag, not in the provider itself. An address
    handed over unchecked is exactly the account-takeover case, so it must not link."""
    from django.contrib.auth import get_user_model

    from core.models import OAuthCredential

    OAuthCredential.objects.create(
        provider="google", client_id="g-id", secret="g-sec", link_by_verified_email=True
    )
    _verified_user("ada@example.com")

    response = _google_signin(Client(), email="ada@example.com", email_verified=False)

    assert response["Location"] != settings.APP_URL
    assert get_user_model().objects.get(email="ada@example.com").has_usable_password()


@pytest.mark.django_db
def test_the_migration_trusts_the_providers_already_registered():
    """Migration 0003 turns linking on for the providers actually in use, so the fix reaches
    production without anyone having to remember a checkbox — while a row added afterwards
    still starts off, keeping a new provider a deliberate decision."""
    import importlib

    from django.apps import apps as global_apps

    from core.models import OAuthCredential

    google = OAuthCredential.objects.create(provider="google", client_id="g", secret="s")
    assert google.link_by_verified_email is False

    migration = importlib.import_module("core.migrations.0003_trust_the_registered_providers")
    migration.trust_registered_providers(global_apps, None)

    google.refresh_from_db()
    assert google.link_by_verified_email is True

    added_later = OAuthCredential.objects.create(provider="github", client_id="x", secret="y")
    assert added_later.link_by_verified_email is False


# --- Sign-in page ---------------------------------------------------------


def test_signin_page_renders_in_the_landing_design():
    """The sign-in page is server-rendered in the landing's look, self-contained (no
    {% static %}), so it renders on a cold instance like the landing does."""
    response = Client().get("/signin")
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/html")
    body = response.content.decode()
    assert "Claim your page" in body
    assert "hirees" in body
    assert "{%" not in body  # every template tag was rendered, none leaked
    # {# #} is single-line only in Django, so a multi-line one prints as page text. That has
    # now bitten twice — the social-signup page, and the messages block in site_base.html.
    assert "{#" not in body
    assert "{% static" not in body


def test_signin_shows_all_three_providers():
    """All three are always shown, so the page reads as a complete sign-in card whatever's
    configured."""
    body = Client().get("/signin").content.decode()
    for name in ("Google", "GitHub", "LinkedIn"):
        assert f"Continue with {name}" in body


def test_signin_unconfigured_provider_routes_to_a_notice_not_an_error():
    """With no credential (CI's state), a provider's button submits back to /signin with
    ?unavailable= rather than to allauth — so a click shows a friendly notice, never a 500."""
    body = Client().get("/signin").content.decode()
    assert 'name="unavailable" value="Google"' in body
    assert "/accounts/google/login/" not in body  # not wired to allauth while unconfigured
    notice = Client().get("/signin?unavailable=Google").content.decode()
    assert "Google sign-in isn't set up yet" in notice


def test_signin_ignores_an_unknown_unavailable_value():
    """The notice renders only for a real provider name — arbitrary input is dropped, so it
    can't be turned into reflected text."""
    body = Client().get("/signin?unavailable=<script>alert(1)</script>").content.decode()
    assert "isn't set up yet" not in body


def test_signin_configured_provider_posts_to_allauth_login():
    """Add a credential and that provider's button becomes a real CSRF-guarded login form."""
    from core.models import OAuthCredential

    OAuthCredential.objects.create(provider="google", client_id="cid", secret="sec")
    body = Client().get("/signin").content.decode()
    assert 'action="/accounts/google/login/?process=login"' in body
    assert "csrfmiddlewaretoken" in body


@pytest.mark.django_db
def test_signin_redirects_an_authenticated_user_to_the_dashboard_spa():
    """Someone already signed in has no reason to see the sign-in page — they're sent to the
    dashboard SPA (app.hirees.me), the same destination as after a fresh sign-in."""
    from django.conf import settings
    from django.contrib.auth.models import User

    user = User.objects.create_user(username="u", password="p")  # noqa: S106 — test-only
    client = Client()
    client.force_login(user)
    response = client.get("/signin")
    assert response.status_code == 302
    assert response["Location"] == settings.LOGIN_REDIRECT_URL == settings.APP_URL


# --- Session endpoint (/api/me) -------------------------------------------
# The dashboard is a separate SPA (app.hirees.me) with no sign-in UI of its own: it calls
# /api/me to learn whether this browser has a backend session, and bounces to /signin if not.


@pytest.mark.django_db
def test_me_reports_anonymous_when_signed_out():
    """No session → the SPA's gate reads authenticated:false and redirects to /signin."""
    response = Client().get("/api/me")
    assert response.status_code == 200
    assert response.json() == {"authenticated": False, "email": "", "display": ""}


@pytest.mark.django_db
def test_me_reports_the_signed_in_user():
    """A live session reports the user, so the SPA renders instead of redirecting."""
    from django.contrib.auth.models import User

    user = User.objects.create_user(username="jo", email="jo@example.com", password="p")  # noqa: S106
    client = Client()
    client.force_login(user)
    body = client.get("/api/me").json()
    assert body["authenticated"] is True
    assert body["email"] == "jo@example.com"


def test_social_signup_does_not_require_email_so_github_never_stalls():
    """GitHub returns no email for private-email users; requiring one would strand them on
    allauth's default signup page. These settings keep social sign-in auto-completing to the
    dashboard SPA (the address is still captured when the provider supplies it)."""
    from django.conf import settings

    assert settings.SOCIALACCOUNT_EMAIL_REQUIRED is False
    assert settings.SOCIALACCOUNT_AUTO_SIGNUP is True


def test_social_signup_page_uses_the_styled_override():
    """allauth's default 'complete signup' page is overridden by our themed template, so
    the social-signup step renders in the site's design, not bare allauth HTML."""
    from django.template.loader import get_template

    origin = get_template("socialaccount/signup.html").origin.name
    assert "templates/socialaccount/signup.html" in origin
    assert "site-packages/allauth" not in origin  # not allauth's default


def test_allauth_pages_inherit_the_styled_base_layout():
    """Every allauth page extends allauth/layouts/base.html; overriding it means no allauth
    page (login-error, logout, email, …) can render as a bare default."""
    from django.template.loader import get_template

    origin = get_template("allauth/layouts/base.html").origin.name
    assert "templates/allauth/layouts/base.html" in origin
    assert "site-packages/allauth" not in origin


# --- Email + password sign-up with code verification ----------------------
# A visitor without a social account can register with an email + password. allauth emails a
# short CODE (not a magic link); the account stays unusable — and never reaches app.hirees.me —
# until the code is entered. Django swaps in the locmem email backend for tests, so the code is
# read straight from mail.outbox.

_STRONG_PASSWORD = "Zephyr-Vault-92"  # noqa: S105 — test-only; clears AUTH_PASSWORD_VALIDATORS
_CODE_RE = re.compile(r"[A-Z]{4}-[A-Z]{4}")  # allauth's dashed 8-char verification code


def _verified_user(email="member@example.com", *, verified=True):
    """A user with a password and an allauth EmailAddress, verified by default."""
    from allauth.account.models import EmailAddress
    from django.contrib.auth.models import User

    user = User.objects.create_user(username=email, email=email, password=_STRONG_PASSWORD)  # noqa: S106
    EmailAddress.objects.create(user=user, email=email, verified=verified, primary=True)
    return user


def _signup(client, email):
    return client.post(
        reverse("account_signup"),
        {"email": email, "password1": _STRONG_PASSWORD, "password2": _STRONG_PASSWORD},
    )


def test_email_verification_is_mandatory_and_by_code():
    """The settings that make the flow safe: local sign-ups must verify, by a code, while
    social stays exempt (its email is already provider-verified)."""
    from django.conf import settings

    assert settings.ACCOUNT_EMAIL_VERIFICATION == "mandatory"
    assert settings.ACCOUNT_EMAIL_VERIFICATION_BY_CODE_ENABLED is True
    assert settings.SOCIALACCOUNT_EMAIL_VERIFICATION == "none"
    assert "password1*" in settings.ACCOUNT_SIGNUP_FIELDS


def test_password_signup_emails_a_code_and_does_not_sign_in_yet():
    """Registering sends one code email and does NOT drop the visitor on the dashboard — the
    account isn't usable until the address is confirmed."""
    from django.conf import settings

    client = Client()
    response = _signup(client, "newbie@example.com")
    assert response.status_code == 302
    assert settings.APP_URL not in response["Location"]  # not sent to the app yet
    assert len(mail.outbox) == 1
    assert _CODE_RE.search(mail.outbox[0].body)  # the email carries a code
    assert client.get("/api/me").json()["authenticated"] is False  # still anonymous


def test_entering_the_code_verifies_and_redirects_to_the_dashboard():
    """The whole point: only after the emailed code is confirmed is the user logged in and
    sent to app.hirees.me (LOGIN_REDIRECT_URL = APP_URL)."""
    from allauth.account.models import EmailAddress
    from django.conf import settings

    client = Client()
    _signup(client, "grace@example.com")
    code = _CODE_RE.search(mail.outbox[0].body).group()
    response = client.post(reverse("account_email_verification_sent"), {"code": code})
    assert response.status_code == 302
    assert response["Location"] == settings.APP_URL
    assert EmailAddress.objects.get(email="grace@example.com").verified is True
    assert client.get("/api/me").json()["authenticated"] is True


def test_a_wrong_code_does_not_verify_or_sign_in():
    """A bad code leaves the address unverified and the visitor off the dashboard."""
    from allauth.account.models import EmailAddress
    from django.conf import settings

    client = Client()
    _signup(client, "mallory@example.com")
    response = client.post(reverse("account_email_verification_sent"), {"code": "ZZZZ-ZZZZ"})
    assert settings.APP_URL not in response.get("Location", "")
    assert EmailAddress.objects.get(email="mallory@example.com").verified is False


def test_unverified_login_is_bounced_to_verification_not_the_app():
    """An account that never confirmed its email can't sign in to the dashboard — the login is
    diverted back to verification, so an unverified address never reaches app.hirees.me."""
    from django.conf import settings

    _verified_user(email="pending@example.com", verified=False)
    response = Client().post(
        reverse("account_login"),
        {"login": "pending@example.com", "password": _STRONG_PASSWORD},
    )
    assert response.status_code == 302
    assert settings.APP_URL not in response["Location"]


def test_a_weak_password_is_rejected():
    """AUTH_PASSWORD_VALIDATORS (added with this flow) block a trivial password: no account is
    created and no code is sent."""
    from django.contrib.auth.models import User

    response = Client().post(
        reverse("account_signup"),
        {"email": "weak@example.com", "password1": "1234", "password2": "1234"},
    )
    assert response.status_code == 200  # form redisplayed with errors
    assert not User.objects.filter(email="weak@example.com").exists()
    assert mail.outbox == []


def test_password_reset_emails_a_link():
    """Forgot-password sends a reset email to an existing account."""
    _verified_user(email="forgetful@example.com")
    response = Client().post(reverse("account_reset_password"), {"email": "forgetful@example.com"})
    assert response.status_code == 302
    assert len(mail.outbox) == 1
    assert "http" in mail.outbox[0].body  # a reset link


def test_signin_hub_offers_email_and_a_create_account_link():
    """The /signin hub carries the email+password form and a link to register, beside the
    social buttons."""
    body = Client().get("/signin").content.decode()
    assert 'name="login"' in body
    assert 'name="password"' in body
    assert 'action="/accounts/login/"' in body
    assert "Create an account" in body
    assert 'href="/accounts/signup/"' in body


def test_email_auth_pages_use_our_themed_templates():
    """login, signup, the code-entry page, and logout render our templates, not allauth's
    bare defaults."""
    from django.template.loader import get_template

    for name in (
        "account/login.html",
        "account/signup.html",
        "account/confirm_email_verification_code.html",
        "account/logout.html",
    ):
        origin = get_template(name).origin.name
        assert "site-packages/allauth" not in origin, name
        assert "/templates/account/" in origin, name


@pytest.mark.django_db
def test_logout_page_renders_a_themed_button():
    """The sign-out confirmation renders in our themed card with the site's .btn button,
    not allauth's bare default one."""
    from django.contrib.auth.models import User

    user = User.objects.create_user(username="bye", password="p")  # noqa: S106 — test-only
    client = Client()
    client.force_login(user)
    response = client.get(reverse("account_logout"))
    assert response.status_code == 200
    body = response.content.decode()
    assert 'class="btn"' in body  # the site's button style
    assert 'action="/accounts/logout/"' in body
    assert "Sign out" in body


@pytest.mark.django_db
def test_logout_redirects_to_the_sign_in_page():
    """Signing out lands on the sign-in page (LOGIN_URL), not the marketing landing, so
    there's an immediate way back in."""
    from django.conf import settings
    from django.contrib.auth.models import User

    user = User.objects.create_user(username="bye2", password="p")  # noqa: S106 — test-only
    client = Client()
    client.force_login(user)
    response = client.post(reverse("account_logout"))
    assert response.status_code == 302
    assert response["Location"] == settings.LOGIN_URL == "/signin"


# --- Django messages ------------------------------------------------------
# The reported symptom: /accounts/logout/ showed "You have signed out." and "Successfully
# signed in as SouhaibBenFarhat." stacked together, above a form asking whether you were
# sure you wanted to sign out. Both were stale. Only the allauth layout drew the queue, so
# messages queued before a redirect to /signin or to the dashboard SPA were never drained —
# they waited in the session for the next allauth page and then all arrived at once.


def test_signing_out_says_so_on_the_page_it_lands_on():
    """The sign-out confirmation is drained by /signin, where it belongs, rather than
    waiting in the session for whatever page the visitor opens next."""
    from django.contrib.auth.models import User

    user = User.objects.create_user(username="msg1", password="p")  # noqa: S106 — test-only
    client = Client()
    client.force_login(user)
    client.post(reverse("account_logout"))

    body = client.get("/signin").content.decode()
    assert "You have signed out." in body
    assert 'class="msgs"' in body


def test_signing_out_leaves_nothing_behind_for_the_next_page():
    """Draining is the whole point: a message must not reappear on a later page. This is the
    screenshot bug — a sign-out notice surfacing above 'Are you sure you want to sign out?'"""
    from django.contrib.auth.models import User

    user = User.objects.create_user(username="msg2", password="p")  # noqa: S106 — test-only
    client = Client()
    client.force_login(user)
    client.post(reverse("account_logout"))
    client.get("/signin")  # drains it here

    assert "You have signed out." not in client.get(reverse("account_logout")).content.decode()


def test_signing_in_queues_no_message_the_dashboard_cannot_show():
    """Sign-in redirects to the SPA at APP_URL, which cannot render Django's message queue,
    so 'Successfully signed in as …' would sit in the session and surface later beside
    something that contradicts it."""
    _verified_user("msg3@example.com")  # unverified would bounce to the code page instead
    client = Client()
    response = client.post(
        reverse("account_login"), {"login": "msg3@example.com", "password": _STRONG_PASSWORD}
    )
    assert response["Location"] == settings.APP_URL, "must actually have signed in"

    assert "Successfully signed in" not in client.get("/signin").content.decode()


# --- Admin branding --------------------------------------------------------


@pytest.mark.django_db
def test_the_admin_is_branded_as_the_product_not_the_repository():
    """`portfolio-backend` is what this service is called in git and on Render. The person
    who opens /admin is looking at hirees.me, and the sidebar should say so."""
    body = Client().get("/admin/login/").content.decode()
    assert "Hirees" in body
    assert "portfolio-backend" not in body


@pytest.mark.django_db
def test_every_sidebar_link_resolves():
    """The sidebar is hand-written, so a renamed or unregistered model breaks it — and
    because it renders on every admin page, that breaks the whole admin, not one link.
    reverse_lazy defers the failure to render time, so force each one here instead."""
    from django.conf import settings

    groups = settings.UNFOLD["SIDEBAR"]["navigation"]
    links = [str(item["link"]) for group in groups for item in group["items"]]
    assert len(links) == 14, "a group changed — check the sidebar still covers what it should"
    assert all(link.startswith("/admin/") for link in links)


@pytest.mark.django_db
def test_the_sidebar_covers_every_model_an_operator_edits():
    """The sidebar is one group per app listing exactly what that app registers, so nothing
    may be absent. An earlier version hid Sites, Groups and OAuth tokens as noise; a map that
    quietly omits things is the same dishonesty as one that points somewhere it doesn't go."""
    from django.conf import settings
    from django.contrib import admin as django_admin

    listed = {
        str(item["link"])
        for group in settings.UNFOLD["SIDEBAR"]["navigation"]
        for item in group["items"]
    }
    missing = {
        f"{m._meta.app_label}.{m._meta.model_name}"
        for m in django_admin.site._registry
        if f"/admin/{m._meta.app_label}/{m._meta.model_name}/" not in listed
    }
    assert missing == set(), f"registered but not in the sidebar: {sorted(missing)}"


@pytest.mark.django_db
def test_every_sidebar_group_has_a_rail_icon():
    """Each group is a button on the icon rail, and the icon is all the button shows. A
    group added without one falls back to a generic circle — indistinguishable from its
    neighbours, which defeats the point of a rail."""
    from django.conf import settings

    groups = settings.UNFOLD["SIDEBAR"]["navigation"]
    icons = [group.get("icon") for group in groups]
    assert all(icons), "every sidebar group needs an icon for the rail"
    assert len(set(icons)) == len(icons), "two groups share an icon — they'd be identical"


@pytest.mark.django_db
def test_the_tile_less_mark_is_served_for_the_rail():
    """The rail is filled with the primary colour, so the tiled favicon would be a petrol
    square on a petrol bar. The glyph variant carries no tile and is recoloured by CSS."""
    response = Client().get("/favicon-glyph.svg")
    body = response.content.decode()
    assert response.status_code == 200
    assert response["Content-Type"] == "image/svg+xml"
    assert "<rect" not in body, "the glyph variant must not carry the tile"
    assert "M5.6 3.4V20.6" in body, "same letter as the tiled mark"


@pytest.mark.django_db
def test_every_sidebar_model_is_themed_and_searchable():
    """Everything the sidebar points at has to look and behave like the rest of the admin.
    Two of these are allauth's own registrations, which arrive as plain django.contrib
    ModelAdmins — unstyled, and in EmailAddress's case unsearchable — so they are
    re-registered in core.admin. This fails if a third-party model is added to the sidebar
    without the same treatment.

    Filters are NOT required: Group has a name and a permissions m2m and nothing worth
    filtering on, and demanding one everywhere would mean inventing filters to satisfy a
    test. Search is different — every list benefits from it."""
    from django.conf import settings
    from django.contrib import admin as django_admin
    from unfold.admin import ModelAdmin as UnfoldModelAdmin

    listed = {
        str(item["link"])
        for group in settings.UNFOLD["SIDEBAR"]["navigation"]
        for item in group["items"]
    }
    for model, model_admin in django_admin.site._registry.items():
        opts = model._meta
        if f"/admin/{opts.app_label}/{opts.model_name}/" not in listed:
            continue
        assert isinstance(model_admin, UnfoldModelAdmin), f"{opts} is not themed"
        assert model_admin.search_fields, f"{opts} has no search box"


# --- Environment-aware URLs ------------------------------------------------


def test_no_url_setting_defaults_to_a_production_host():
    """A default is what you get with NO environment, and the only environment with none is
    a laptop. APP_URL defaulted to https://app.hirees.me, and LOGIN_REDIRECT_URL is APP_URL —
    so every sign-in on localhost ended on the production dashboard.

    Run in a subprocess with the environment stripped, because by the time settings are
    imported here a default and an explicit value are indistinguishable. That is also why
    this is a test and not a system check: a check cannot see which one it got.
    """
    import subprocess
    import sys

    script = (
        "import django, os; os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings');"
        "django.setup();"
        "from django.conf import settings;"
        "print('|'.join(f'{n}={getattr(settings, n)}' for n in settings.URL_SETTINGS))"
    )
    # A cleared environment, minus what Python itself needs to start.
    env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "")}
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, env=env, check=True
    )

    for pair in result.stdout.strip().split("|"):
        name, _, value = pair.partition("=")
        assert any(host in value for host in ("localhost", "127.0.0.1")), (
            f"{name} defaults to {value!r}. Defaults are for laptops; production sets its "
            f"own value in render.yaml."
        )


@pytest.mark.django_db
def test_localhost_urls_fail_the_checks_in_production():
    """FRONTEND_URL kept its localhost default in production for weeks, because nothing
    reads it until a social login has already failed — at which point the visitor was sent
    to a page that exists only on a developer's machine. `manage.py check` runs during
    Render's build, so this turns that class of bug into a failed deploy."""
    from django.test import override_settings

    from core.checks import check_urls_are_not_local_in_production

    with override_settings(DEBUG=False, APP_URL="http://localhost:5173"):
        ids = [error.id for error in check_urls_are_not_local_in_production(None)]
    assert "core.E001" in ids

    with override_settings(
        DEBUG=False, APP_URL="https://app.hirees.me", FRONTEND_URL="https://app.hirees.me"
    ):
        ids = [error.id for error in check_urls_are_not_local_in_production(None)]
    assert "core.E001" not in ids

    # Silent while developing — the whole point is that localhost is correct there.
    with override_settings(DEBUG=True, APP_URL="http://localhost:5173"):
        assert check_urls_are_not_local_in_production(None) == []


def test_the_api_docs_aim_at_the_environment_you_are_running():
    """Swagger's "Try it out" fires at whichever server is listed first. With production
    pinned at the top, requests sent from a developer's own /api/docs/ hit the live service —
    including the rating PUT and the chat POST, which write.

    Asserted against the committed spec rather than settings.SPECTACULAR_SETTINGS, because
    Django's test runner forces DEBUG off while that list was ordered at import under the
    real value. CI regenerates the spec with DEBUG=true, so localhost leads there — which is
    also the copy a developer reads.
    """
    import yaml

    servers = yaml.safe_load(Path("openapi.yaml").read_text())["servers"]
    assert "localhost" in servers[0]["url"]
    assert any("onrender.com" in server["url"] for server in servers)


def test_the_error_redirect_follows_the_dashboard():
    """FRONTEND_URL has one consumer and no host of its own, so it cannot drift from the
    app it points into — which is exactly how it went wrong before."""
    assert settings.FRONTEND_URL == settings.APP_URL
    assert settings.HEADLESS_FRONTEND_URLS["socialaccount_login_error"].startswith(settings.APP_URL)


def test_the_site_and_the_admin_read_one_palette():
    """The tokens were declared in four places and drifted every time one changed. Both
    ends now derive from core/tokens.py, so a change to the accent or the ink cannot move
    one and leave the other."""
    from core.tokens import DARK, LIGHT, site_css

    css = site_css()
    assert settings.UNFOLD["COLORS"]["primary"]["600"] == LIGHT["accent"]
    assert settings.UNFOLD["COLORS"]["primary"]["500"] == DARK["accent"]
    assert settings.UNFOLD["COLORS"]["font"]["default-light"] == LIGHT["text"]
    assert settings.UNFOLD["COLORS"]["font"]["default-dark"] == DARK["text"]
    assert f"--accent: {LIGHT['accent']}" in css
    assert f"--text: {DARK['text']}" in css


def test_the_rendered_tokens_carry_both_dark_mode_routes():
    """Dark has to be emitted twice — a media query for the operating system and an
    attribute for the toggle — because CSS cannot share one block between them. Losing
    either breaks half the theming, silently."""
    from core.tokens import site_css

    css = site_css()
    assert "@media (prefers-color-scheme: dark)" in css
    assert ':root:not([data-theme="light"])' in css
    assert ':root[data-theme="dark"]' in css


@pytest.mark.django_db
def test_the_public_pages_render_their_tokens_inline():
    """No stylesheet: these pages must draw correctly on a cold instance with nothing
    collected, which is why the tokens are inlined by a tag rather than served as a file."""
    from core.tokens import LIGHT

    for path in ("/", "/signin"):
        body = Client().get(path).content.decode()
        assert f"--accent: {LIGHT['accent']}" in body, f"{path} lost its tokens"
        assert "{%" not in body and "{#" not in body, f"{path} leaked a template tag"


# --- Rows per page ---------------------------------------------------------


@pytest.mark.django_db
def test_the_default_page_size_is_small_enough_to_paginate():
    """Unfold's 100 meant most lists had exactly one page, which made the pagination
    controls decorative. 24 fills a screen and leaves the paging meaningful."""
    from django.conf import settings
    from django.contrib import admin as django_admin

    from core.paging import DEFAULT_PAGE_SIZE

    assert DEFAULT_PAGE_SIZE == 24
    listed = {
        str(item["link"])
        for group in settings.UNFOLD["SIDEBAR"]["navigation"]
        for item in group["items"]
    }
    for model, model_admin in django_admin.site._registry.items():
        opts = model._meta
        assert f"/admin/{opts.app_label}/{opts.model_name}/" in listed, f"{opts} is unlisted"
        assert model_admin.list_per_page == DEFAULT_PAGE_SIZE, f"{opts} opted out"


@pytest.mark.django_db
def test_choosing_a_page_size_sticks_across_tables():
    """A page size is a property of how you read a table, not of the table — so it is stored
    once, per operator, and every list follows it."""
    from django.contrib.auth import get_user_model

    from core.paging import SESSION_KEY

    staff = get_user_model().objects.create_superuser(
        "pager", "pager@example.com", "Zephyr-Vault-92"
    )  # noqa: S106 — test-only
    client = Client()
    client.force_login(staff)

    client.get("/admin/auth/user/?per_page=96")
    assert client.session[SESSION_KEY] == 96
    # A different model entirely, with no parameter of its own.
    other = client.get("/admin/core/profile/")
    assert other.context["cl"].list_per_page == 96


@pytest.mark.django_db
def test_a_junk_page_size_falls_back_instead_of_breaking_the_page():
    """The value arrives in a URL anyone can edit or a stale bookmark can carry. A broken
    page size must never be able to break the page."""
    from django.contrib.auth import get_user_model

    from core.paging import DEFAULT_PAGE_SIZE

    staff = get_user_model().objects.create_superuser("junk", "junk@example.com", "Zephyr-Vault-92")  # noqa: S106 — test-only
    client = Client()
    client.force_login(staff)

    for bad in ("nonsense", "-5", "99999", ""):
        response = client.get(f"/admin/auth/user/?per_page={bad}")
        assert response.status_code == 200, f"{bad!r} broke the changelist"
        assert response.context["cl"].list_per_page == DEFAULT_PAGE_SIZE


@pytest.mark.django_db
def test_the_page_size_is_not_treated_as_a_filter():
    """Anything in the query string the admin doesn't recognise is treated as a field
    lookup, so without stripping this one, ?per_page=48 raised IncorrectLookupParameters —
    the admin's error page — instead of paginating."""
    from django.contrib.auth import get_user_model

    staff = get_user_model().objects.create_superuser("filt", "filt@example.com", "Zephyr-Vault-92")  # noqa: S106 — test-only
    client = Client()
    client.force_login(staff)

    response = client.get("/admin/auth/user/?per_page=48&is_staff__exact=1")
    assert response.status_code == 200
    assert response.context["cl"].list_per_page == 48


@pytest.mark.django_db
def test_switching_size_keeps_the_search_and_drops_the_page_number():
    """Losing your filters because you asked for more rows would make the control worse than
    useless. The page number goes, though: page 7 of a 24-row list is not page 7 of a 96."""
    from django.contrib.auth import get_user_model

    staff = get_user_model().objects.create_superuser("keep", "keep@example.com", "Zephyr-Vault-92")  # noqa: S106 — test-only
    client = Client()
    client.force_login(staff)

    body = client.get("/admin/auth/user/?q=demo&p=3").content.decode()
    i = body.find("changelist-page-size")
    control = body[i : i + 800]
    assert "q=demo" in control, "the search was dropped"
    assert "p=3" not in control, "the page number was carried over"


@pytest.mark.django_db
def test_the_caption_matches_the_rows_actually_on_the_page():
    """The caption is computed from cl.result_list, and this asserts that against the rows
    the page really renders — the two disagreeing is exactly the bug that looks like the
    admin lying to you about what it is showing."""
    import re

    from django.contrib.auth import get_user_model

    from chat.models import LLMCredential

    staff = get_user_model().objects.create_superuser("cap", "cap@example.com", "Zephyr-Vault-92")  # noqa: S106 — test-only
    for i in range(30):
        LLMCredential.objects.create(provider="mistral", label=f"k{i}", api_key=f"sk-{i}")

    client = Client()
    client.force_login(staff)
    body = client.get("/admin/chat/llmcredential/?per_page=24").content.decode()

    rows = body.count('class="data-row')
    caption = re.search(r'changelist-total">([^<]+)', body).group(1)
    caption = " ".join(caption.split())

    assert rows == 24, f"page rendered {rows} rows"
    assert caption == "Showing 1 to 24 of 30 API credentials", caption

    # Second page: the range moves, the total does not.
    body = client.get("/admin/chat/llmcredential/?per_page=24&p=2").content.decode()
    caption = " ".join(re.search(r'changelist-total">([^<]+)', body).group(1).split())
    assert body.count('class="data-row') == 6
    assert caption == "Showing 25 to 30 of 30 API credentials", caption
