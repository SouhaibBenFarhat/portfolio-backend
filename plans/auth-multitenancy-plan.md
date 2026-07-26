# Auth & Multi-tenancy — Implementation Plan

Take `portfolio-backend` from **single-tenant** (Souhaib's) to **multi-tenant hirees.me**:
users sign in with Google / GitHub / LinkedIn, own their content (CV, facts, hosted page),
and manage it from a React dashboard. Recruiters chat with any user's hosted page.

## Resolved decisions

| Concern | Choice | Why |
| --- | --- | --- |
| Identity | **django-allauth**, headless mode, self-hosted | Data stays in our EU Postgres (same reason as Mistral); no per-user cost; Django already owns identity |
| Providers | Google, GitHub, **LinkedIn (OpenID Connect)** | The three fast paths for a recruiter/dev audience |
| Dashboard | **React SPA at `app.hirees.me`** on the DRF API | Reuses the OpenAPI→TypeScript contract already in place |
| Admin | Stays **staff-only** (Unfold), untouched | It's an operator console, not a product surface |
| SPA↔API auth | **Token** (headless "app" mode) — *not* a `.hirees.me` cookie | A parent-domain cookie leaks to every tenant page → XSS steals sessions (see `docs/infrastructure.md`) |
| Tenancy | `Profile` (1:1 `User`) owns content; `owner` FK on `Document`/`Fact`/`Conversation` | Least-change path to isolation; LLM keys + `ChatModel` chain stay **platform**-owned |

## Three surfaces

- **Public tenant page** — `souhaib.hirees.me` (or `/u/souhaib`). No login. The product's output.
- **Dashboard** — `app.hirees.me`. Login required. Where users land after sign-in.
- **Staff admin** — `/admin`. Operators only. Unchanged.

## Phases (one branch each, squash-merged)

### Phase 1 — Accounts + social login (backend) ← building now
- Install `django-allauth` + `allauth.headless` + `django.contrib.sites`.
- Wire Google / GitHub / LinkedIn, credentials read from env (`*_CLIENT_ID` / `*_SECRET`),
  falling back gracefully when unset (a provider with no key just isn't offered).
- Expose headless endpoints for the SPA (`/_allauth/…`) + the provider callback routes
  (`/accounts/<provider>/login/callback/`) the OAuth apps already point at.
- Token auth for the "app" client so the cross-subdomain SPA never needs a shared cookie.
- **No tenancy yet** — existing content stays global; this phase only proves users can sign
  in and the backend issues a session/token. Tests for the flow. CI stays green (allauth
  endpoints aren't DRF, so `openapi.yaml` doesn't change).

### Phase 2 — Tenancy / ownership (backend, the real foundation)
- `Profile` (1:1 `User`) with a unique **handle** (their public-page slug).
- Add `owner` FK to `Document`, `Fact`, `Conversation`; data migration backfills Souhaib as
  tenant #1 and assigns existing rows to him.
- Scope the agent tools, `/chat/stream`, restore, and rating endpoints to a **tenant**
  resolved from the request (subdomain or `/u/<handle>`). LLM keys + `ChatModel` stay global.
- Reserve `www`, `app`, `api`, `admin`, `mail`, `status`, `docs` as non-claimable handles.

### Phase 3 — Public tenant pages
- Resolve tenant → load *their* CV/facts into the existing chat widget; render the hosted page.
- `souhaib.hirees.me` becomes the first real tenant page.

### Phase 4 — Dashboard SPA (`app.hirees.me`)
- Auth-gated React app: onboarding (upload CV → extract → facts), manage documents/facts,
  view conversations + ratings, settings (handle, publish toggle). Reuses generated TS types.

### Phase 5 — Productionize
- `ALLOWED_HOSTS` + CORS/CSRF for `.hirees.me`; per-tenant rate limits + token quotas;
  billing later.

## Cross-cutting

- **External setup already done** (see `docs/infrastructure.md`): OAuth apps registered
  (GitHub/Google/LinkedIn), `api.hirees.me` pointed at Render.
- **Least privilege carries over** — the chat's read-only tools mean a hijacked model still
  can't touch another tenant's data once ownership scoping (Phase 2) lands.
- **CI gates unchanged** — ruff, `manage.py check`, migration-drift, OpenAPI-drift, pytest.
