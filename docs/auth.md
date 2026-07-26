# Authentication — sign-in

How visitors sign in to hirees.me — social login **and** email + password — where the provider
credentials live, how the OAuth apps are registered, and how the verification-code email is sent.
Domain/DNS/hosting is in `docs/infrastructure.md`; the build roadmap is in
`plans/auth-multitenancy-plan.md`.

## How it works

- **django-allauth**, self-hosted. Two ways in, side by side: **social login** (Google, GitHub,
  LinkedIn) and **email + password**. For social, sign-in and sign-up are one action — the first
  "Continue with…" creates the account, later ones just log in. Email + password has a real
  sign-up form and requires a verification code (see "Email + password sign-in" below).
- The entry pages are **server-rendered in the site's own design** (not allauth's default
  templates): the landing hero leads with a one-click **Continue with Google**, and **`/signin`**
  shows all three providers. After sign-in a visitor is redirected to the **dashboard SPA at
  `app.hirees.me`** (`LOGIN_REDIRECT_URL`); **sign-out** returns to the public landing.
- allauth handles the OAuth callback on the backend. The callback path is
  `/accounts/<provider>/login/callback/` — for LinkedIn it's `/accounts/oidc/linkedin/login/callback/`.

Key files: `core/views.py` (`landing`, `signin`, `me`), `core/templates/core/signin.html`,
the themed allauth pages in `templates/account/` + `templates/allauth/`, `core/models.py`
(`OAuthCredential`), `core/adapter.py`, and the auth/email blocks in `config/settings.py`.

## Email + password sign-in

Alongside social login, visitors can register with an email and password. Because this is a
public endpoint, a fake or mistyped address must never reach the product — so allauth is set to
**mandatory email verification by code** (`ACCOUNT_EMAIL_VERIFICATION="mandatory"` +
`ACCOUNT_EMAIL_VERIFICATION_BY_CODE_ENABLED=True`): on sign-up allauth emails a short code, the
visitor types it back, and only **then** is the account verified, logged in, and redirected to
`app.hirees.me` (`LOGIN_REDIRECT_URL`). An unverified login is bounced back to the code page — it
never reaches the dashboard. The flow: sign-up form → emailed code → enter code → signed in →
`app.hirees.me`.

Social sign-in stays **exempt** (`SOCIALACCOUNT_EMAIL_VERIFICATION="none"`) — its email is already
provider-verified — so the one-click social flow is unchanged.

- **Entry:** `/signin` carries the email+password form (posts to allauth's `account_login`) and a
  "Create an account" link, beside the social buttons. The pages — login, sign-up, code entry,
  password reset, password change — are allauth's own views, **themed** by overriding
  `templates/account/*` on top of `templates/allauth/layouts/base.html` (the same base the social
  pages use).
- **Anti-abuse** (the free tier is public): `AUTH_PASSWORD_VALIDATORS` reject weak passwords;
  `ACCOUNT_RATE_LIMITS` cap sign-ups, failed logins, and code resends per IP; the code expires in
  15 minutes with a 3-attempt cap. Verification-by-code is itself the anti-fake-email control.
- **Password reset** ("forgot password") and change are the same allauth views, themed, using the
  same sender.

### Sending the mail (Brevo)

The code and reset links go out over SMTP via **Brevo** (EU, free tier — same data-residency
reason as Mistral). Config is env-driven (`EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_USE_TLS`,
`EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `DEFAULT_FROM_EMAIL`; see `render.yaml` /
`.env.example`). **With `EMAIL_HOST` unset the app uses Django's console backend** — the code
prints to the server log, so the whole flow is testable locally with nothing sent.

Deliverability is DNS-dependent: on Cloudflare, add Brevo's **SPF**, **DKIM**, and a **DMARC**
record for `hirees.me`, and verify `no-reply@hirees.me` as a Brevo sender — otherwise codes land in
spam. Owner setup before production email sign-up works: create the Brevo account + those DNS
records, and set `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` in the Render dashboard.

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

Now that **email + password** accounts exist, an email can be owned by a local account too, which
raises the stakes. The conservative default is already in place — `SOCIALACCOUNT_EMAIL_AUTHENTICATION`
is `False`, so a social login does **not** silently authenticate into an existing local account by
email match. Still tracked before sign-in goes public (Phase 2): add an explicit `pre_social_login`
guard that refuses to link onto an existing account via an **unverified** provider email, and never
auto-grant `is_staff`. This was deliberately left out of the email+password change to avoid touching
the working social flow without provider-level integration testing.

## Session strategy — how the SPA knows you're signed in

The backend owns all auth; the dashboard SPA (`app.hirees.me`, a separate Render Static Site)
has **no sign-in UI of its own**. On load it calls **`GET /api/me`** with `credentials: 'include'`
and reads the `authenticated` flag: signed in → it renders; signed out → it redirects the browser
to the backend's **`/signin`**. After sign-in the backend redirects back to the SPA
(`LOGIN_REDIRECT_URL = APP_URL`).

This runs on an **ordinary host-only session cookie** on `hirees.me` — **not** a parent-domain
cookie. `app.hirees.me` and `hirees.me` share the registrable domain `hirees.me`, so a fetch from
the SPA to the backend is **same-site**, and a `SameSite=Lax` cookie rides along on it. Because
the cookie is scoped to the `hirees.me` host (not `.hirees.me`), it is **never sent to tenant
subdomains** (`souhaib.hirees.me`) — so `SESSION_COOKIE_DOMAIN = ".hirees.me"` stays **rejected**
(an XSS on a tenant page still can't reach a platform session). CORS allows the `app.hirees.me`
origin with credentials (`CORS_ALLOW_CREDENTIALS`), and `CSRF_TRUSTED_ORIGINS` covers the SPA for
the cross-origin POSTs it will make later (chat, ratings).

**Token auth** (allauth headless "app" mode — the `/_allauth/` API is already mounted) stays a
clean future alternative if a fully cookieless SPA is ever wanted, but the same-site host-only
cookie above needs no tokens and no auth code in the SPA beyond the gate.
