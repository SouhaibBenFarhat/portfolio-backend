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
from dotenv import load_dotenv

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
    "corsheaders",
    "rest_framework",
    "drf_spectacular",
    "drf_spectacular_sidecar",  # vendored Swagger UI / Redoc assets (offline, via whitenoise)
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
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
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
    "http://localhost:4321,https://souhaibbenfarhat.github.io",
)
CORS_ALLOW_ALL_ORIGINS = env_bool("CORS_ALLOW_ALL_ORIGINS", default=False)

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
    "SITE_TITLE": "portfolio-backend",
    "SITE_HEADER": "portfolio-backend",
    "SITE_ICON": "/favicon.svg",  # the service's own favicon route (core.views.favicon)
    # Match the portfolio site's design tokens (src/styles/global.css in the frontend
    # repo) so the admin reads as part of the same product. The site's two accent
    # values sit on the shades Unfold actually renders: primary-600 is the light-mode
    # accent (--accent #1f6f78), primary-500 the dark-mode one (dark --accent #5cb6be);
    # the base scale runs from the site's warm off-whites (--bg, --line) into its
    # dark blue-grays (--line/--surface/--bg in dark mode).
    "COLORS": {
        # Derived from the site's tokens, with the light/dark plane deltas widened:
        # the site's literal values (#faf8f4 vs #ffffff, #14171c vs #0d0f12) are close
        # enough for small bordered cards, but across the admin's large flat bands
        # (header, sidebar, footer) they read as one merged surface.
        "base": {
            # NOTE: base-50/base-950 are used by Unfold internally for row striping and
            # hover tints, so they must stay near the surface tone — the page and field
            # planes get their own literal values in core/static/core/unfold-overrides.css.
            "50": "#f8f5ef",  # subtle tint (zebra rows, hovers) — a whisper off white
            "100": "#eee8da",
            "200": "#d8cfba",  # borders — must be visible, not a whisper
            "300": "#c0b8a6",
            "400": "#99a0ac",  # site dark --muted
            "500": "#7c828e",
            "600": "#5b616d",  # site --muted: quiet body text
            "700": "#3a4049",
            "800": "#333c49",  # dark-mode borders
            "900": "#1e242e",  # dark surfaces — a full step above the page, not a hint
            "950": "#13171e",  # the dark page — a step below the surfaces, not pitch black
        },
        "primary": {
            "50": "#eef7f8",
            "100": "#d8edef",
            "200": "#b9dfe3",
            "300": "#93cfd5",
            "400": "#78c3ca",
            "500": "#5cb6be",  # site dark --accent
            "600": "#1f6f78",  # site --accent: restrained petrol/teal
            "700": "#1a5f68",
            "800": "#14525a",
            "900": "#0e4a51",  # site --accent-ink
            "950": "#08272a",  # the site's dark solid-button ink
        },
        # Unfold's default body text is base-600 in light mode — our base-600 is the
        # site's *muted* tone, too quiet for whole paragraphs. One step darker reads
        # like the site's ink while captions/help text stay on the muted tones.
        "font": {
            "subtle-light": "var(--color-base-500)",
            "subtle-dark": "var(--color-base-400)",
            "default-light": "var(--color-base-700)",
            "default-dark": "var(--color-base-300)",
            "important-light": "var(--color-base-900)",
            "important-dark": "var(--color-base-100)",
        },
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
    "SERVERS": [
        {"url": "https://portfolio-backend-2huw.onrender.com", "description": "Production"},
        {"url": "http://localhost:8000", "description": "Local development"},
    ],
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
