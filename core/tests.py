import pytest
import yaml
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
def test_signin_redirects_an_authenticated_user_onward():
    """Someone already signed in has no reason to see the sign-in page."""
    from django.contrib.auth.models import User

    user = User.objects.create_user(username="u", password="p")  # noqa: S106 — test-only
    client = Client()
    client.force_login(user)
    response = client.get("/signin")
    assert response.status_code == 302


# --- Dashboard stub (the signed-in destination) ---------------------------


@pytest.mark.django_db
def test_dashboard_requires_login():
    """/app is the signed-in area — an anonymous visitor is sent to /signin."""
    response = Client().get("/app")
    assert response.status_code == 302
    assert "/signin" in response["Location"]


@pytest.mark.django_db
def test_dashboard_greets_the_user_and_offers_sign_out():
    """Signed in, the stub greets the user by their account email and posts a sign-out form
    to allauth's logout — the far end of the loop we're proving works."""
    from django.contrib.auth.models import User

    user = User.objects.create_user(username="jo", email="jo@example.com", password="p")  # noqa: S106
    client = Client()
    client.force_login(user)
    body = client.get("/app").content.decode()
    assert "You're in" in body
    assert "jo@example.com" in body
    assert 'action="/accounts/logout/"' in body  # the sign-out form
    assert "csrfmiddlewaretoken" in body


def test_social_signup_does_not_require_email_so_github_never_stalls():
    """GitHub returns no email for private-email users; requiring one would strand them on
    allauth's default signup page. These settings keep social sign-in auto-completing to /app
    (the address is still captured when the provider supplies it)."""
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
