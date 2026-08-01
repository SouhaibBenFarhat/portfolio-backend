"""
Django settings for the portfolio backend.

Configuration is driven entirely by environment variables so the same code runs
locally and in production. Safe defaults let it boot with zero setup for local dev;
production values are injected by the host (see render.yaml).
"""

import base64
import hashlib
import os
from pathlib import Path

import dj_database_url
from django.templatetags.static import static
from django.urls import reverse_lazy
from dotenv import load_dotenv

from core.tokens import ADMIN_COLORS

BASE_DIR = Path(__file__).resolve().parent.parent

# Load a local .env for development. Existing env vars win, so production values
# injected by the host (Render) are never overridden.
load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


# --- Core -----------------------------------------------------------------
SECRET_KEY = os.getenv("SECRET_KEY", "dev-insecure-key-do-not-use-in-prod")
DEBUG = env_bool("DEBUG", default=True)
ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", "localhost,127.0.0.1,.onrender.com")

# Session-recording snapshot POSTs (proxied to PostHog at /ingest/s/) can be large —
# PostHog recommends allowing ~64MB. Django's 2.5MB default rejects them with a 400
# before they reach PostHog, which silently breaks Session Replay through the proxy.
DATA_UPLOAD_MAX_MEMORY_SIZE = 64 * 1024 * 1024  # 64 MB

INSTALLED_APPS = [
    # Admin theme. Must precede django.contrib.admin: its app config swaps admin.site
    # for the themed UnfoldAdminSite before autodiscovery runs — loaded later, every
    # model registration would land on the stock site and the admin would be empty.
    "unfold",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",  # required by allauth (SITE_ID below)
    "corsheaders",
    "rest_framework",
    "drf_spectacular",
    "drf_spectacular_sidecar",  # vendored Swagger UI / Redoc assets (offline, via whitenoise)
    # Auth / social login. allauth.headless exposes the REST endpoints the dashboard SPA
    # calls; the provider apps add Google / GitHub / LinkedIn (the last via generic OpenID
    # Connect — see the auth section below for why).
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",
    "allauth.socialaccount.providers.github",
    "allauth.socialaccount.providers.openid_connect",
    "allauth.headless",
    "core",
    "analytics_proxy",
    "chat",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "allauth.account.middleware.AccountMiddleware",  # required by allauth
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],  # project-level overrides (searched before app dirs)
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]

# Postgres in production (DATABASE_URL, set on Render), SQLite locally when unset.
# conn_max_age keeps connections warm; conn_health_checks avoids reusing dead ones.
DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=600,
        conn_health_checks=True,
    )
}

# --- Static files ---------------------------------------------------------
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

# --- i18n -----------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# --- CORS -----------------------------------------------------------------
# The frontend (GitHub Pages) calls this API cross-origin, so its origin must be allowed.
CORS_ALLOWED_ORIGINS = env_list(
    "CORS_ALLOWED_ORIGINS",
    # localhost:4321 = the Astro site (dev); localhost:5173 = the dashboard SPA (Vite dev);
    # app.hirees.me = the deployed dashboard SPA, which calls this backend with credentials.
    "http://localhost:4321,http://localhost:5173,"
    "https://souhaibbenfarhat.github.io,https://app.hirees.me",
)
CORS_ALLOW_ALL_ORIGINS = env_bool("CORS_ALLOW_ALL_ORIGINS", default=False)
# The dashboard SPA reads the backend session cross-origin (credentials: 'include'), so the
# response must be allowed to carry credentials for its origin.
CORS_ALLOW_CREDENTIALS = env_bool("CORS_ALLOW_CREDENTIALS", default=True)
# Cross-origin POSTs from the SPA (same-site: app.hirees.me → hirees.me) need their origin
# trusted for Django's CSRF check on unsafe methods. GET /api/me doesn't, but chat/rating
# POSTs from the SPA will — trust them here now.
CSRF_TRUSTED_ORIGINS = env_list(
    "CSRF_TRUSTED_ORIGINS",
    "http://localhost:5173,https://hirees.me,https://app.hirees.me",
)

# --- Authentication / social login (allauth) ------------------------------
# Users sign in with Google, GitHub, or LinkedIn (self-hosted via django-allauth). The client
# id/secret for each are managed in the Django admin as *encrypted* OAuthCredential rows (see
# core.models / core.adapter) — never in settings, env, or allauth's plaintext SocialApp
# table. See docs/infrastructure.md for how the OAuth apps are registered, and
# plans/auth-multitenancy-plan.md for the roadmap.
SITE_ID = 1

# --- Public URLs ----------------------------------------------------------------------
# Every setting naming a host outside this service. They are together, and named in
# URL_SETTINGS below, so that one list drives the system check and the tests rather than
# three places each keeping their own idea of what counts as a URL setting.
#
# THE RULE, which is the file's own docstring applied properly: a default is what you get
# with NO environment, and the only environment with none is a developer's laptop. So every
# default here points at localhost, and production supplies the real value through
# render.yaml. Both directions of getting this wrong were live in this file:
#
#   APP_URL defaulted to https://app.hirees.me, and LOGIN_REDIRECT_URL is APP_URL — so
#   every sign-in on localhost ended on the production dashboard.
#
#   FRONTEND_URL defaulted to localhost and was NOT in render.yaml, so production used the
#   laptop value: a social-login failure there redirected the visitor to
#   http://localhost:4321/auth/error. It sat like that for weeks because nothing looks at
#   this setting unless something has already gone wrong.
#
# core.checks now fails `manage.py check` when DEBUG is off and any of these still points
# at localhost, so the second kind can't reach production again.

# The dashboard SPA (a separate Render Static Site at app.hirees.me in production). The
# backend owns all auth: after sign-in it redirects here, and the SPA — which has NO sign-in
# UI of its own — gates on the backend session and bounces unauthenticated visitors back to
# /signin. Vite's dev server port locally. See docs/auth.md.
APP_URL = os.getenv("APP_URL", "http://localhost:5173")

# Where allauth sends the browser when a social round-trip fails. Its only consumer is
# HEADLESS_FRONTEND_URLS, which serves the SPA — so it DEFAULTS TO APP_URL rather than
# carrying a host of its own. That is the fix for how it broke: an independent setting that
# nothing reads until something else has gone wrong will keep whatever value it was born
# with, and nobody will notice. Tied to APP_URL it cannot drift, and production needs no
# extra variable. Override it only if the error page ever lives somewhere else.
FRONTEND_URL = os.getenv("FRONTEND_URL", APP_URL)

# Read by core.checks and by the tests. Add a URL setting to this list when you add one.
URL_SETTINGS = ("FRONTEND_URL", "APP_URL")
URL_LIST_SETTINGS = ("CORS_ALLOWED_ORIGINS", "CSRF_TRUSTED_ORIGINS")

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",  # admin / staff login
    "allauth.account.auth_backends.AuthenticationBackend",  # social + email login
]

# Email is the identifier (no username). Two ways in, side by side:
#  - Social (Google/GitHub/LinkedIn): the account arrives with a provider-verified email, so
#    it's exempt from our verification step (SOCIALACCOUNT_EMAIL_VERIFICATION="none" below).
#  - Email + password: the visitor sets a password and must prove the address is real before
#    the account works — the mandatory by-code verification below.
# New-style allauth 65 settings — the pre-65 ACCOUNT_EMAIL_REQUIRED / ACCOUNT_USERNAME_REQUIRED
# names are deprecated.
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*", "password2*"]

# Local (email+password) sign-ups must confirm their address before the account is usable.
# allauth emails a short CODE (not a magic link) that the visitor types back, so a fake or
# mistyped address can never reach the product and an unverified login is bounced to the code
# page rather than to APP_URL. Social stays exempt (its email is already provider-verified),
# so the one-click social flow is unchanged.
ACCOUNT_EMAIL_VERIFICATION = "mandatory"
SOCIALACCOUNT_EMAIL_VERIFICATION = "none"
ACCOUNT_EMAIL_VERIFICATION_BY_CODE_ENABLED = True
ACCOUNT_EMAIL_VERIFICATION_BY_CODE_MAX_ATTEMPTS = 3  # wrong-code tries before the code is void
ACCOUNT_EMAIL_VERIFICATION_BY_CODE_TIMEOUT = 15 * 60  # seconds a code stays valid
ACCOUNT_EMAIL_SUBJECT_PREFIX = ""  # no "[example.com]" prefix on our transactional mail

# Abuse caps on the public auth endpoints, merged over allauth's already-sane defaults: cap
# mass fake-account creation, brute force against a known email, and code-resend spamming.
# This is the cost/anti-scammer control layered on top of the verification code itself.
ACCOUNT_RATE_LIMITS = {
    "signup": "10/m/ip",
    "login_failed": "5/5m/key",
    "confirm_email": "1/30s/key",  # cooldown between code (re)sends
    "reset_password": "5/m/ip,3/m/key",
}

# GitHub returns no email for users whose email is private; a required email would then stall
# them on allauth's default "complete signup" page instead of landing on the dashboard. Don't
# require email for social sign-in — auto-create the account from whatever the provider gives
# (allauth still pulls the address from GitHub's /user/emails when the user:email scope grants
# it), so the flow completes straight through. (Phase 2, multi-tenancy, revisits identity.)
SOCIALACCOUNT_EMAIL_REQUIRED = False
SOCIALACCOUNT_AUTO_SIGNUP = True

# Someone who signed up with email+password and later clicks "Continue with Google" is the
# same person, and should land on the same account rather than be told it already exists.
# WHICH providers may do that is decided per credential row in the admin
# (OAuthCredential.link_by_verified_email → the app's `email_authentication` setting), not
# here: every OpenID Connect service shares the provider id "openid_connect", so a global
# flag could not trust LinkedIn without also trusting the next OIDC provider added. This
# setting only says what happens once such a login is allowed — write the SocialAccount
# link. Without it allauth signs them in but stores nothing, so it re-matches on the email
# every single time and sign-in breaks the day they change their address.
SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT = True

# The auth flow is server-rendered for now — a /signin page and an /app dashboard stub, in
# the site's design — so allauth serves its classic endpoints (provider login/callback and
# logout) that those pages post to. HEADLESS_ONLY stays False; when the dashboard becomes a
# separate SPA (Phase 4) it can flip to headless — the /_allauth/ API is already mounted.
HEADLESS_ONLY = env_bool("HEADLESS_ONLY", default=False)
HEADLESS_FRONTEND_URLS = {"socialaccount_login_error": f"{FRONTEND_URL}/auth/error"}
# After sign-in, land on the dashboard SPA (app.hirees.me). LOGIN_URL is the backend's own
# Django sign-in page — where the SPA sends unauthenticated visitors — and sign-out returns
# there too (the sign-in page, not the marketing landing), so signing out leads straight back
# to a way back in.
LOGIN_REDIRECT_URL = APP_URL
LOGIN_URL = "/signin"
ACCOUNT_LOGOUT_REDIRECT_URL = LOGIN_URL


# Provider-level config (scopes) only. Client credentials do NOT live here — they're the
# encrypted OAuthCredential rows in the admin, fed to allauth by core.adapter. LinkedIn needs
# no entry: it's wired through the generic openid_connect provider (callback
# /accounts/oidc/linkedin/login/callback/, not the dead linkedin_oauth2 one) and its issuer
# URL travels with its credential row.
SOCIALACCOUNT_PROVIDERS = {
    "google": {"SCOPE": ["profile", "email"], "AUTH_PARAMS": {"access_type": "online"}},
    "github": {"SCOPE": ["read:user", "user:email"]},
}
# Feed allauth its apps from the encrypted OAuthCredential model rather than env/plaintext.
SOCIALACCOUNT_ADAPTER = "core.adapter.SocialAccountAdapter"
# Drops the "successfully signed in" message, which is queued right before the browser is
# redirected to APP_URL — a separate app that cannot render Django's message queue, so it
# would sit in the session and surface later on an unrelated page. See core.adapter.
ACCOUNT_ADAPTER = "core.adapter.AccountAdapter"

# Password strength for email+password sign-ups (Django's standard validators). There was no
# reason for these before — social sign-ups set no password — so they're added with this flow.
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --- Email ----------------------------------------------------------------
# Transactional mail: the verification code and password-reset links. Brevo (EU, free tier)
# over SMTP in production, configured from env vars. When EMAIL_HOST is unset the console
# backend prints the message to the server log instead — so the verification code is readable
# in local dev with zero setup, and no real mail is sent until Brevo is wired.
EMAIL_HOST = os.getenv("EMAIL_HOST", "")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", default=True)
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
EMAIL_BACKEND = (
    "django.core.mail.backends.smtp.EmailBackend"
    if EMAIL_HOST
    else "django.core.mail.backends.console.EmailBackend"
)
# One async worker sends this inline during the request, so don't let a slow SMTP server hang
# it. From address stays on the hirees.me domain so DKIM/SPF align (see docs/auth.md).
EMAIL_TIMEOUT = 10
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "hirees.me <no-reply@hirees.me>")
SERVER_EMAIL = DEFAULT_FROM_EMAIL

# --- AI chat --------------------------------------------------------------
# LiteLLM model ids. The chat tries CHAT_MODEL first; if it fails (e.g. a free-tier
# quota/rate limit) before any text streams, it falls back to CHAT_FALLBACK_MODEL.
# The provider prefix picks the API key env var ("groq/..." → GROQ_API_KEY, etc.).
CHAT_MODEL = os.getenv("CHAT_MODEL", "mistral/mistral-small-latest")
CHAT_FALLBACK_MODEL = os.getenv("CHAT_FALLBACK_MODEL", "mistral/open-mistral-nemo")
# Higher = more varied and conversational; lower = more focused/repetitive.
CHAT_TEMPERATURE = float(os.getenv("CHAT_TEMPERATURE", "0.7"))

# GitHub, for the project/README tools. A token is optional but lifts the API rate
# limit from 60/hour (anonymous) to 5000/hour.
GITHUB_USERNAME = os.getenv("GITHUB_USERNAME", "SouhaibBenFarhat")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")

# --- Tenancy --------------------------------------------------------------
# The domain tenant pages hang off, so souhaib.hirees.me resolves to the tenant "souhaib".
# Only the label directly under this domain counts, and only when the rest matches exactly —
# so app.hirees.me and the onrender.com hostname resolve to nobody, and a host we don't
# serve can't smuggle a handle in.
TENANT_BASE_DOMAIN = os.getenv("TENANT_BASE_DOMAIN", "hirees.me")
# The handle given to tenant #1 when ownership was introduced — the account that already
# owned every document, fact and conversation on this instance. Read from the environment
# rather than hardcoded in the migration for the same reason as the chat-model seed
# (chat/migrations/0010): another instance running this code has a different first user,
# and a migration that assumed ours would hand them a stranger's name.
DEFAULT_TENANT_HANDLE = os.getenv("DEFAULT_TENANT_HANDLE", "souhaib")

# Requests that name no tenant — the Astro portfolio posting to the apex host, or a local
# curl — are answered as this handle. It is what keeps the public chat on
# souhaibbenfarhat.github.io working unchanged now that conversations belong to someone.
# Set it empty to make an unresolvable request a 404 instead, once every tenant reaches
# their page through their own subdomain.
FALLBACK_TENANT_HANDLE = os.getenv("FALLBACK_TENANT_HANDLE", DEFAULT_TENANT_HANDLE)

# Fernet key for encrypting secrets at rest (LLM API keys stored in the admin).
# Derived from SECRET_KEY so there's no separate env var to manage. Note: rotating
# SECRET_KEY makes existing encrypted values unreadable (just re-enter the keys).
FIELD_ENCRYPTION_KEY = base64.urlsafe_b64encode(hashlib.sha256(SECRET_KEY.encode()).digest())

# Guardrails for the public chat endpoint.
CHAT_MAX_MESSAGE_LENGTH = int(os.getenv("CHAT_MAX_MESSAGE_LENGTH", "2000"))  # chars
# How much history is replayed to the model each turn. Models are stateless, so the whole
# thread is resent every request — this bounds cost and keeps the prompt inside the context
# window. Set well below the window (131k tokens) so a long chat visibly fills the client's
# context gauge without ever truncating mid-conversation.
CHAT_MAX_HISTORY_MESSAGES = int(os.getenv("CHAT_MAX_HISTORY_MESSAGES", "100"))
# A conversation's token budget. The whole thread is resent to the model every turn, so a
# long one costs more each time; past this the thread is spent and refuses new messages
# (the client shows a full gauge and invites starting a new chat). Keep it below the
# model's own context window — it's clamped to that anyway.
CHAT_MAX_CONTEXT_TOKENS = int(os.getenv("CHAT_MAX_CONTEXT_TOKENS", "20000"))
# Rate limit: at most CHAT_RATE_LIMIT requests per IP per CHAT_RATE_WINDOW_SECONDS
# (default 10 per minute). The per-IP key is the caller's real Cloudflare client IP —
# see chat.views._client_ip for why X-Forwarded-For can't be trusted for this.
CHAT_RATE_LIMIT = int(os.getenv("CHAT_RATE_LIMIT", "10"))
CHAT_RATE_WINDOW_SECONDS = int(os.getenv("CHAT_RATE_WINDOW_SECONDS", "60"))

# Mistral free-tier monthly token allowance (~1 billion; env-tunable). Used only to render
# the "quota used" percentage in the admin: Mistral exposes no usage API to our tier, so the
# app counts tokens itself (chat.views records them per model) and compares to this ceiling.
MISTRAL_FREE_TOKENS_PER_MONTH = int(os.getenv("MISTRAL_FREE_TOKENS_PER_MONTH", "1000000000"))

# --- Chat scope check -----------------------------------------------------
# A cheap model call reads the visitor's message before the agent runs and refuses ones
# that aren't about Souhaib, so the free tier can't be spent on someone using the chat as a
# general assistant. Checking the message costs a fraction of generating the answer and
# then reviewing it, which is why it happens at this end. See chat/guard.py.
CHAT_GUARD_ENABLED = env_bool("CHAT_GUARD_ENABLED", default=True)
# Deliberately the CHAT_MODEL env var and not the chain's head: this is a one-word
# classifier, so it has no reason to run on whatever expensive model is answering.
CHAT_GUARD_MODEL = os.getenv("CHAT_GUARD_MODEL", CHAT_MODEL)

# --- Follow-up suggestions ------------------------------------------------
# After each reply, a cheap model call writes up to 3 questions the visitor could ask
# next, streamed as a `suggestions` frame and rendered as tappable chips. A recruiter
# doesn't know what the assistant can answer — the chips do the prompting for them.
# See chat/suggestions.py.
CHAT_SUGGESTIONS_ENABLED = env_bool("CHAT_SUGGESTIONS_ENABLED", default=True)
# Pinned to the CHAT_MODEL env var, not the chain's head, for the same reason as
# CHAT_GUARD_MODEL: a chip writer has no reason to run on whatever expensive model is
# answering.
CHAT_SUGGESTIONS_MODEL = os.getenv("CHAT_SUGGESTIONS_MODEL", CHAT_MODEL)

# --- Admin (Unfold theme) -------------------------------------------------
UNFOLD = {
    # The product's name, not the repository's. "portfolio-backend" is what this service is
    # called in git and on Render; the thing an operator is looking at when they open /admin
    # is hirees.me. SITE_HEADER is the sidebar heading, SITE_TITLE the browser tab.
    "SITE_TITLE": "Hirees",
    "SITE_HEADER": "Hirees",
    "SITE_ICON": "/favicon.svg",  # the service's own favicon route (core.views.favicon)
    # Match the portfolio site's design tokens (src/styles/global.css in the frontend
    # repo) so the admin reads as part of the same product. The site's two accent
    # values sit on the shades Unfold actually renders: primary-600 is the light-mode
    # accent (--accent #1f6f78), primary-500 the dark-mode one (dark --accent #5cb6be);
    # the base scale runs from the site's warm off-whites (--bg, --line) into its
    # dark blue-grays (--line/--surface/--bg in dark mode).
    # The palette is core/tokens.py — the same module the public pages render their
    # `:root` block from, so the accent, the ink and the type cannot drift between the two
    # ends of the product. Only the neutral ramp is the admin's own; the reasoning for that
    # lives with the values.
    "COLORS": ADMIN_COLORS,
    # --- Sidebar --------------------------------------------------------------
    # Without this, Unfold lists every registered model grouped by Django app, alphabetically.
    # That produced a sidebar nobody could read: "users", "email addresses", "social accounts"
    # and "profiles" sat next to each other with nothing saying how they differ, "social
    # application tokens" and "sites" appeared despite never being touched, and the two things
    # an operator actually opens most — documents and facts — were buried under "chat".
    #
    # So the navigation is written out by hand, grouped by the job you came to do rather than
    # by which app happens to define the model, and the labels say what the row IS:
    #
    #   Pages       = core.Profile        one per tenant: their handle, and whether it's live
    #   Accounts    = auth.User           the sign-in identity — one person can have several
    #                                     email addresses and several linked providers
    #   Email addresses / Linked providers = allauth's records hanging off an account
    #
    # THE TRADE-OFF: a hand-written list does not grow by itself, so a newly registered model
    # would be invisible until someone adds it here. Unfold's "All applications" link was the
    # first answer to that and it was removed: with only three models outside the groups it
    # printed the same list a second time, which is noise rather than a safety net. The guard
    # is now a test — core.tests.test_the_sidebar_covers_every_model_an_operator_edits pins
    # exactly which models are absent, so registering a fourth without placing it turns CI red
    # rather than quietly hiding it. Deliberately left out: auth.Group (no roles here — the
    # only accounts are the owner's), sites.Site (allauth requires SITE_ID but nothing edits
    # it), and socialaccount.SocialToken (raw OAuth tokens; reading them helps nobody).
    #
    # Every group is `collapsible`, so the sidebar opens as five headings rather than eleven
    # links: click one and its items unfold beneath it. Unfold expands whichever group holds
    # the page you're currently on, so navigating never leaves you facing a closed list with
    # no idea where you are. Five short lists you choose between beat one long list you have
    # to read past — which was the actual complaint.
    #
    # No per-item `permission` callbacks: every item is visible to any staff account, and
    # Django still enforces the real check on the page itself, so a link they can't use is a
    # 403 rather than a leak. Worth revisiting the day staff means more than one person.
    "SIDEBAR": {
        "show_search": True,
        "show_all_applications": False,  # see the note above on what guards the list instead
        "navigation": [
            {
                "title": "Tenants",
                "icon": "groups",
                "separator": True,
                "items": [
                    {
                        "title": "Pages",
                        "icon": "public",
                        "link": reverse_lazy("admin:core_profile_changelist"),
                    },
                    {
                        "title": "Accounts",
                        "icon": "person",
                        "link": reverse_lazy("admin:auth_user_changelist"),
                    },
                ],
            },
            {
                "title": "What the assistant knows",
                "icon": "menu_book",
                "separator": True,
                "items": [
                    {
                        "title": "Documents",
                        "icon": "description",
                        "link": reverse_lazy("admin:chat_document_changelist"),
                    },
                    {
                        "title": "Facts",
                        "icon": "help",
                        "link": reverse_lazy("admin:chat_fact_changelist"),
                    },
                ],
            },
            {
                "title": "Chat",
                "icon": "forum",
                "separator": True,
                "items": [
                    {
                        "title": "Conversations",
                        "icon": "forum",
                        "link": reverse_lazy("admin:chat_conversation_changelist"),
                    },
                    {
                        "title": "Models",
                        "icon": "smart_toy",
                        "link": reverse_lazy("admin:chat_chatmodel_changelist"),
                    },
                    {
                        "title": "Token usage",
                        "icon": "monitoring",
                        "link": reverse_lazy("admin:chat_tokenusage_changelist"),
                    },
                ],
            },
            {
                "title": "Sign-in",
                "icon": "login",
                "separator": True,
                "items": [
                    {
                        "title": "Social login apps",
                        "icon": "login",
                        "link": reverse_lazy("admin:core_oauthcredential_changelist"),
                    },
                    {
                        "title": "Email addresses",
                        "icon": "mail",
                        "link": reverse_lazy("admin:account_emailaddress_changelist"),
                    },
                    {
                        "title": "Linked providers",
                        "icon": "link",
                        "link": reverse_lazy("admin:socialaccount_socialaccount_changelist"),
                    },
                ],
            },
            {
                "title": "Keys",
                "icon": "key",
                "separator": True,
                "items": [
                    {
                        "title": "API credentials",
                        "icon": "vpn_key",
                        "link": reverse_lazy("admin:chat_llmcredential_changelist"),
                    },
                ],
            },
        ],
    },
    # Elevation bridge: Unfold is flat (page, cards, and fields share backgrounds);
    # this sheet recreates the site's page → surface → field plane system. It only
    # consumes the tokens above, so the palette stays defined in this one file.
    # The URL is stamped with the file's mtime: browsers cache static files, and a
    # stale copy of this sheet silently un-themes the whole admin (fresh HTML +
    # old CSS renders as broken zebra striping and missing surfaces).
    "STYLES": [
        lambda request: (
            static("core/unfold-overrides.css")
            + f"?v={int((BASE_DIR / 'core/static/core/unfold-overrides.css').stat().st_mtime)}"
        )
    ],
}

# --- REST framework + OpenAPI docs ----------------------------------------
# The JSON endpoints are DRF views so drf-spectacular can introspect them into an
# OpenAPI 3 schema. The API is machine-facing (the frontend is the consumer), so we
# render JSON only — the human-readable interface is Swagger UI at /api/docs/.
REST_FRAMEWORK = {
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],  # public API
    # No authentication: every endpoint is public, so don't advertise basic/session
    # auth in the schema (it would be misleading and adds needless CSRF surface).
    "DEFAULT_AUTHENTICATION_CLASSES": [],
}

SPECTACULAR_SETTINGS = {
    "TITLE": "portfolio-backend API",
    "DESCRIPTION": (
        "Backend for souhaibbenfarhat.github.io — service/health endpoints and the "
        "streaming AI chat assistant (Server-Sent Events). The PostHog analytics proxy "
        "at /ingest/* is an opaque pass-through and is intentionally not documented here."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,  # don't list the schema endpoint inside the schema
    # Serve Swagger UI / Redoc from the vendored sidecar assets, not a CDN.
    "SWAGGER_UI_DIST": "SIDECAR",
    "SWAGGER_UI_FAVICON_HREF": "SIDECAR",
    "REDOC_DIST": "SIDECAR",
    # /chat/stream is an async Server-Sent Events view, which DRF can't model, so it's
    # injected into the schema by a postprocessing hook (see chat/schema.py).
    "POSTPROCESSING_HOOKS": [
        "drf_spectacular.hooks.postprocess_schema_enums",
        "chat.schema.add_chat_stream_path",
    ],
    # Ordered by environment, because Swagger UI's "Try it out" fires at whichever server
    # is FIRST. With production pinned at the top, every request sent from a developer's
    # own /api/docs/ went to the live service — including the rating PUT and the chat POST,
    # which write. The docs should aim at the thing you are running.
    "SERVERS": (
        [
            {"url": "http://localhost:8000", "description": "Local development"},
            {"url": "https://portfolio-backend-2huw.onrender.com", "description": "Production"},
        ]
        if DEBUG
        else [
            {"url": "https://portfolio-backend-2huw.onrender.com", "description": "Production"},
            {"url": "http://localhost:8000", "description": "Local development"},
        ]
    ),
}

# --- Logging ---------------------------------------------------------------
# Everything goes to stderr, because stderr is what the host captures: Render shows it in
# the service's log stream. Deliberately not a file (the free disk is ephemeral) and not a
# database table (the free Postgres is small — RequestLog is already pruned on every single
# request for exactly that reason).
#
# Django's own default is a complete no-op in production, and that cost real debugging time:
# its `console` handler carries a `require_debug_true` filter, and its `mail_admins` handler
# returns immediately because ADMINS is empty. So an unhandled 500 — a failing SMTP send
# during an OAuth callback, a DisallowedHost, any view raising — wrote nothing anywhere, and
# a bare "Server Error (500)" in the browser was the only evidence it had happened.
# Redefining `django` here with propagate=False replaces both of those handlers.
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    # Render stamps its own timestamp on every line it captures, so adding one here would
    # just be duplication in the place these logs are actually read.
    "formatters": {"console": {"format": "{levelname} {name}: {message}", "style": "{"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "console"}},
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        # django.request has no entry of its own in Django's defaults — it propagates here,
        # carrying the traceback on 5xx and a plain line on 4xx.
        "django": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
    },
}

# --- Production hardening --------------------------------------------------
if not DEBUG:
    # Render terminates TLS at its edge and forwards X-Forwarded-Proto.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = True
    SECURE_HSTS_SECONDS = 31_536_000  # 1 year
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
