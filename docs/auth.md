# Authentication — social sign-in

How visitors sign in to hirees.me, where the provider credentials live, and how the OAuth
apps are registered. Domain/DNS/hosting is in `docs/infrastructure.md`; the build roadmap is
in `plans/auth-multitenancy-plan.md`.

## How it works

- **django-allauth**, self-hosted, **social-login only** — Google, GitHub, LinkedIn. Sign-in
  and sign-up are one action: the first "Continue with…" creates the account, later ones just
  log in (there is no separate sign-up form — this is the standard OAuth pattern).
- The entry pages are **server-rendered in the site's own design** (not allauth's default
  templates): the landing hero leads with a one-click **Continue with Google**, and **`/signin`**
  shows all three providers. After sign-in a visitor lands on **`/app`** (a dashboard stub for
  now); **sign-out** returns to the public landing.
- allauth handles the OAuth callback on the backend. The callback path is
  `/accounts/<provider>/login/callback/` — for LinkedIn it's `/accounts/oidc/linkedin/login/callback/`.

Key files: `core/views.py` (`signin`, `dashboard`, `landing`), `core/templates/core/{signin,dashboard}.html`,
`core/models.py` (`OAuthCredential`), `core/adapter.py`, and the auth block in `config/settings.py`.

## Credentials live in the admin, encrypted

The client id + secret for each provider are **managed in the Django admin under "Social login
apps"**, with the secret **Fernet-encrypted at rest** (the same protection as
`chat.LLMCredential`) — never in env vars, settings, or allauth's plaintext `SocialApp` table.
A custom adapter (`core.adapter.SocialAccountAdapter`) feeds them to allauth, decrypting only in
memory; allauth's own `SocialApp` admin is hidden so credentials can only ever take the encrypted
path. This is also **how production keys are set** — add the rows in the deployed `/admin`, no
Render env vars and no redeploy.

**Add a provider** — `/admin` → *Social login apps* → *Add*:

| Provider | `provider`        | `provider_id` | `server_url`                       | also        |
| -------- | ----------------- | ------------- | ---------------------------------- | ----------- |
| Google   | `google`          | —             | —                                  | id + secret |
| GitHub   | `github`          | —             | —                                  | id + secret |
| LinkedIn | `openid_connect`  | `linkedin`    | `https://www.linkedin.com/oauth`   | id + secret |

An unconfigured provider **still shows its button**, but clicking it routes to a friendly
"…isn't set up yet — please try another method" notice instead of erroring (see
`core.views.signin` / `_configured_provider_ids`; the notice only renders for a real provider
name, so it can't be turned into reflected text).

## The OAuth apps (registered 2026-07-26)

Each provider needs an app on its own developer console. Client **secrets are in the owner's
password manager** and entered into the admin; only identifiers and callbacks are recorded here.

**Callbacks must match the exact host the visitor is on.** allauth builds the redirect URI
from the request host, and each provider only allows pre-registered redirect URLs — a
mismatch is the `redirect_uri_mismatch` / "not associated with this application" error. The
app is served from the **apex `hirees.me`** (not `api.hirees.me` — an early wrong assumption;
`www.hirees.me` just redirects to the apex), so the production callback is
`https://hirees.me/accounts/<provider>/login/callback/`, with `http://localhost:8000/...`
alongside for local dev (except GitHub — see below). Register every host the app is reached
on (`hirees.me`, and `localhost` for dev); `127.0.0.1` counts as a different host from
`localhost`.

- **GitHub** — <github.com/settings/developers>, app `hirees.me`, callback
  `https://hirees.me/accounts/github/login/callback/`. **Gotcha:** a GitHub **OAuth App** allows
  only **one** callback URL (unlike Google/LinkedIn, which take several), so it can't cover prod
  *and* localhost at once — use a separate OAuth App per environment (or a GitHub *App*, which
  allows multiple). GitHub thus can't be tested on localhost while the one callback points at
  prod. (Distinct from the `GITHUB_TOKEN` the chat's repo tools use — a different credential.)
- **Google** — Google Cloud Console, project `hirees`; consent screen audience **External**;
  OAuth client type **Web application**; both the production and localhost redirect URIs on the
  one client. Callback `.../accounts/google/login/callback/`.
- **LinkedIn** — <linkedin.com/developers/apps>, app `hirees.me`. Required a LinkedIn **Company
  Page** first (`hirees.me`: Company / Software Development / size 0–1 / Privately Held). Product
  **"Sign In with LinkedIn using OpenID Connect"**, access-token lifetime 2 months.
  **Wired through allauth's generic `openid_connect` provider, NOT `linkedin_oauth2`** — that
  provider still calls LinkedIn's removed `/v2/me` + `r_liteprofile` endpoints, which the OpenID
  Connect product doesn't grant. So the callback is `/accounts/oidc/linkedin/login/callback/`,
  and the LinkedIn redirect URLs **must** use that `oidc/linkedin` path (prod + localhost) or
  sign-in returns a `redirect_uri` mismatch.

## Account linking — a sharp edge to tighten before public launch

allauth links a social login to an **existing account with the same email**. That's why the
owner signing in with LinkedIn lands on their existing Django **superuser** account and can
reach `/admin` — expected for the owner, but a **security risk in general**: if a provider
ever returns an email the account owner never verified, an attacker could take over a matching
account. A brand-new email just creates a plain user (`is_staff=False`, no `/admin`).

Before sign-in is public (Phase 2), tighten this: require provider-verified emails for linking,
or disable auto-link entirely (`SOCIALACCOUNT_EMAIL_AUTHENTICATION` / a custom adapter), and
never auto-grant `is_staff`.

## Session strategy — the app/api subdomain split (open decision)

The dashboard SPA (`app.hirees.me`, Phase 4) and the API (`api.hirees.me`) will be **different
subdomains**. A parent-domain cookie (`.hirees.me`) is sent to *every* tenant page too, so an XSS
on any tenant page could steal a platform session — therefore `SESSION_COOKIE_DOMAIN = ".hirees.me"`
is **rejected**. Two clean options for when the SPA lands:

1. **Token auth** — allauth headless "app" mode: the SPA holds a token, no cross-subdomain cookie
   needed. Public tenant pages need no auth at all. *(Preferred — sidesteps cookies entirely.)*
2. **Same-origin API** — serve the API under the dashboard host so a host-only cookie works and
   never reaches tenant subdomains.

Until the SPA exists the flow is **server-rendered** (`HEADLESS_ONLY=false`) with an ordinary
session cookie scoped to the backend host. The `/_allauth/` headless API is already mounted for
the SPA to adopt later.
