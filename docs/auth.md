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
  `app.hirees.me`** (`LOGIN_REDIRECT_URL`); **sign-out** returns to `/signin`
  (`ACCOUNT_LOGOUT_REDIRECT_URL = LOGIN_URL`), so signing out leads straight back to a way in.
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

Deliverability runs off a branded subdomain (`mail.hirees.me`) plus DKIM (×2), a return-path, a
`brevo-code` ownership TXT, and a DMARC record on Cloudflare — **the seven records, and how they
were added (via the Cloudflare API, not the flaky dashboard), are in `docs/infrastructure.md` →
"Email deliverability (Brevo)"**. Every CNAME is grey-cloud / **DNS only** (a proxied DKIM CNAME
silently breaks signing). Those records are live and **Brevo reports the domain authenticated
(2026-07-26)**.

**Production email is live (2026-07-29):** the Brevo SMTP credentials are set in Render, so the
verification-code and password-reset mail now sends over Brevo instead of falling back to the
console backend. `EMAIL_HOST` (`smtp-relay.brevo.com`), `EMAIL_PORT` (`587`), `EMAIL_USE_TLS`, and
`DEFAULT_FROM_EMAIL` come from the Blueprint; `EMAIL_HOST_USER` (the Brevo SMTP login) and
`EMAIL_HOST_PASSWORD` (an SMTP key generated under Brevo → *SMTP & API → SMTP*) are `sync: false`
secrets, so they live only in the Render dashboard, never in the repo. Local dev still leaves
`EMAIL_HOST` unset and uses the console backend. Note: a Brevo SMTP key **expires after 90 days of
inactivity** — if production email ever stops, regenerate the key and update `EMAIL_HOST_PASSWORD`.

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

## Account linking — one person, one account

Someone who signs up with email + password and later clicks "Continue with Google" is the same
person, and lands on the **same account**. Both routes keep working from then on.

Before this, they didn't. allauth's default is to refuse: it emailed an **"Account Already
Exists"** notice and dropped the visitor on a confirm-email page instead of signing them in.
Two things were wrong with that. It's a dead end — nobody remembers which button they used
months ago. And because the notice is an *email*, a mail server that's down turned the whole
OAuth callback into a **500** (nothing catches the send; see "When mail is down" below).

**Whether a provider may do this is per credential row**, not a global switch:
*Social login apps* → **Link by verified email**. allauth reads the app's
`email_authentication` setting before any global one, which is the only way to express it —
every OpenID Connect service shares the provider id `openid_connect`, so a settings-level flag
could not trust LinkedIn without also trusting the next OIDC provider added. `core.adapter`
carries the row's flag onto the in-memory `SocialApp`.

Two guards make this safe, and they are the reason it isn't simply on for everyone:

- **The provider's `verified` flag, not the provider.** allauth only ever matches on addresses
  the provider reports as verified (`adapter.authenticate_by_email`). Google and LinkedIn assert
  `email_verified` in the id token; GitHub's `user:email` scope reports it per address. A
  provider that hands back an address it never checked is exactly how an attacker would walk
  into someone else's account — hence **off by default** for any row nobody has assessed.
  Migration `core/0003` turns it on for the three providers already registered; a row added
  later starts off, so adding a provider stays a deliberate decision.
- **The password is wiped if the local address was never verified.** That's allauth's
  `wipe_password`: if an attacker registered your address and never confirmed it, they know a
  password to an account you're about to be linked into. Wiping locks them out. Our own sign-ups
  verify by code, so a real user's password **survives** and both routes keep working.

`SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT = True` writes the `SocialAccount` link on the
first such sign-in. Without it allauth signs the visitor in but stores nothing, so it re-matches
on the address every single time — and sign-in breaks the day they change their email.

Note the `is_staff` consequence is unchanged and expected: the owner's account *is* the Django
superuser, so signing in with a linked provider reaches `/admin`. A brand-new email still just
creates a plain user (`is_staff=False`). Nothing here ever grants staff.

### When mail is down

allauth sends transactional mail **inline during the request** — the verification code, the
password-reset link, the "account already exists" notice. Every one of those sends used to be
unguarded, so a broken SMTP server didn't degrade a flow, it raised straight out of the view.
**Seven paths answered `Server Error (500)`**: sign-up, password reset, an unverified login,
and both OAuth callbacks. That is what the reported 500s on
`/accounts/google/login/callback/` and `/accounts/github/login/callback/` actually were —
mail failing, not OAuth.

`core.adapter.AccountAdapter.send_mail` now catches, logs the traceback, and adds a visible
message so the visitor isn't left refreshing an inbox. The flow continues. **Nothing in the
auth surface 500s on a mail outage** — there's a test per path.

That makes an outage quiet, so watch for it deliberately: grep the logs for
`Could not send`. The one flow that is *functionally* broken without mail is email + password
sign-up, since the account can't be verified without the code. Social sign-in is unaffected —
it sends no mail at all.

### GitHub, and why its emails have to be fetched

`SOCIALACCOUNT_QUERY_EMAIL` is set **explicitly on**. allauth defaults it to
`SOCIALACCOUNT_EMAIL_REQUIRED`, which is off here (so a GitHub user with a private address
isn't stranded — see above), and that silently switched off the `/user/emails` call too. The
`user:email` scope was still requested and granted; the result was just never read.

The effect was subtle and bad: a GitHub login arrived carrying only the **public profile**
address, marked **unverified**. Linking requires a verified address, so a GitHub sign-in could
never match an existing account no matter what the credential row said — it always fell into
the "account already exists" branch, and therefore always into the mail path. With it on,
GitHub's verified addresses are read, including the private ones the public profile omits.

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
