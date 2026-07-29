import re

import pytest
import yaml
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


def test_favicon_uses_the_small_mark_and_the_full_one_is_available():
    """The tab renders at 16px, so /favicon.svg serves the variant without text lines."""
    small = Client().get("/favicon.svg").content.decode()
    full = Client().get("/favicon-full.svg").content.decode()
    assert small.count("<path") < full.count("<path")
    assert "linearGradient" not in small  # the old gradient monogram is gone
    assert "#1f6f78" in small  # the site's petrol accent


def test_favicon_is_served_as_svg():
    response = Client().get("/favicon.ico")
    assert response.status_code == 200
    assert response["Content-Type"] == "image/svg+xml"
    assert b"<svg" in response.content


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
