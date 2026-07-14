# portfolio-backend

Django API powering the backend of [souhaibbenfarhat.github.io](https://souhaibbenfarhat.github.io).

The portfolio frontend is a static Astro site on GitHub Pages, which can't run
server code. This service is that server side — deployed separately and called over
HTTPS. First iteration ships two things:

1. **Health/service endpoints** — a deployable, monitored running server.
2. **A first-party PostHog reverse proxy** — so analytics survives ad-blockers.

Next iterations will add the AI assistant chat (LLM-backed, streamed over SSE).

## Architecture

```
Browser ──► Astro site (GitHub Pages, static)
   │
   └──► portfolio-backend (Django on Render)
              ├── /health              liveness
              └── /ingest/*  ──► PostHog EU   (first-party analytics proxy)
```

## Endpoints

| Method | Path              | Purpose                                                        |
| ------ | ----------------- | -------------------------------------------------------------- |
| GET    | `/`               | Service descriptor (JSON)                                      |
| GET    | `/health`         | Liveness probe (`{"status":"ok"}`), used by Render             |
| GET    | `/api/docs/`      | Swagger UI — interactive API documentation                     |
| GET    | `/api/schema/`    | OpenAPI 3 schema (the contract; feeds frontend type generation)|
| POST   | `/chat/stream`    | Streaming AI chat (Server-Sent Events)                         |
| GET    | `/chat/conversations/<uuid>/` | Restore a stored conversation                      |
| ANY    | `/ingest/<path>`  | Reverse proxy to PostHog (see below)                           |

## API documentation (OpenAPI / Swagger)

The JSON endpoints are Django REST Framework views; [drf-spectacular](https://drf-spectacular.readthedocs.io/)
introspects them into an **OpenAPI 3** schema:

- **Swagger UI:** [`/api/docs/`](https://portfolio-backend-2huw.onrender.com/api/docs/) —
  interactive, human-readable docs (assets vendored via `drf-spectacular-sidecar`, so no CDN).
- **Schema:** [`/api/schema/`](https://portfolio-backend-2huw.onrender.com/api/schema/) — the
  raw OpenAPI document. Also committed to the repo as [`openapi.yaml`](./openapi.yaml).

`/chat/stream` is an async Server-Sent Events endpoint, which DRF can't model, so it's
described by hand in [`chat/schema.py`](./chat/schema.py) and injected into the schema by a
postprocessing hook (its SSE frames become named component schemas). `/ingest/*` is an opaque
proxy and is intentionally left out of the docs.

**The committed `openapi.yaml` is the contract.** CI regenerates it and fails on any drift
(the same guard as the migrations check), so the spec can never silently fall behind the code.
Regenerate locally after changing an endpoint:

```bash
python manage.py spectacular --file openapi.yaml --validate
```

### Frontend type generation (contract-driven)

The [frontend](https://github.com/SouhaibBenFarhat/souhaibbenfarhat.github.io) generates its
TypeScript types from this spec (`openapi-typescript`), so the two repos can't drift apart.
On a push to `main` that changes `openapi.yaml`, this repo fires a `repository_dispatch` at the
frontend, whose workflow regenerates the types and opens a PR when they change.

### PostHog reverse proxy

Ad-blockers drop requests to `posthog.com`. Routing analytics through our own domain
makes them first-party, so they aren't blocked. The proxy mirrors PostHog's
recommended layout:

```
/ingest/static/*  ──►  https://eu-assets.i.posthog.com/static/*   (library assets)
/ingest/*         ──►  https://eu.i.posthog.com/*                 (events, decide, …)
```

The real client IP is forwarded (`X-Forwarded-For`) so geolocation stays accurate.

Point the frontend at it:

```js
posthog.init(KEY, {
  api_host: "https://<your-service>.onrender.com/ingest",
  ui_host: "https://eu.posthog.com",
});
```

### Proxy gotchas — why Session Replay silently broke

Getting the proxy right for **Session Replay** took debugging three separate bugs. Each
one produces the same confusing symptom: **events work, but no recordings appear** —
because events run from the site's own JS bundle, while the replay recorder is lazy-loaded
*from the proxy*.

1. **Compression — don't forward the browser's `Accept-Encoding`.** PostHog's CDN serves
   the recorder script as **brotli/zstd**, which Python `requests` can't decode. Two ways
   to get this wrong: forwarding the compressed bytes without `Content-Encoding` (corrupt
   JS in every browser), or forwarding them *with* `Content-Encoding: br` (works in Chrome
   but **Safari renders it as corrupt `�` bytes** — WebKit's decoder is stricter). Either
   way the recorder gets stuck in `lazy_loading` and nothing records. The fix: **drop the
   browser's `Accept-Encoding`** so `requests` negotiates plain gzip with the upstream,
   decodes it, and the proxy emits **plain, un-encoded JavaScript** that every browser
   (Safari included) reads.
2. **Large request bodies must be allowed.** Recording snapshots exceed Django's 2.5 MB
   `DATA_UPLOAD_MAX_MEMORY_SIZE` default, which returns a Django 400 *before* the request
   reaches PostHog. Raised to **64 MB** (PostHog's recommendation) in settings.
3. **Frontend persistence.** On the site, `posthog-js` must use `localStorage+cookie`
   (not `memory`) persistence, or replay silently does not record.

Regression tests in [`analytics_proxy/tests.py`](./analytics_proxy/tests.py) lock in (1)
and (2): a >2.5 MB POST must reach upstream, and `Content-Encoding` must be preserved.
Diagnosing it was fastest via a temporary `window.__ph = posthog` hook to read
`posthog.sessionRecording.status` in the browser console.

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python manage.py runserver
# → http://127.0.0.1:8000/health
```

## Configuration

All via environment variables (safe local defaults built in):

| Variable               | Default (local)                                             | Notes                              |
| ---------------------- | ---------------------------------------------------------- | ---------------------------------- |
| `DEBUG`                | `true`                                                     | Must be `false` in production      |
| `SECRET_KEY`           | dev fallback                                              | Render generates its own           |
| `ALLOWED_HOSTS`        | `localhost,127.0.0.1,.onrender.com`                       | Comma-separated                    |
| `CORS_ALLOWED_ORIGINS` | `http://localhost:4321,https://souhaibbenfarhat.github.io` | Origins allowed to call the API    |

## Tests & linting

```bash
ruff check . && ruff format --check .
pytest -q
```

CI (GitHub Actions) runs lint, format check, Django checks, a migrations-drift check,
and the test suite on every push and PR to `main`.

**Deploys are gated on the tests.** The Render build runs `pytest` as part of its
`buildCommand` (see [`render.yaml`](./render.yaml)): if any test fails, the build fails,
the deploy is aborted, and the previously-deployed version stays live. A red test suite
can't reach production — this is what the reverse-proxy regression tests above protect.

## Deployment (Render)

This repo ships a [`render.yaml`](./render.yaml) Blueprint.

1. Push to GitHub.
2. In Render → **New + → Blueprint** → select this repo → **Apply**.
3. Render provisions the service, generates `SECRET_KEY`, and deploys.

`autoDeploy` is on, so every push to `main` redeploys.

**Live:** https://portfolio-backend-2huw.onrender.com

## Keeping the free instance awake

Render's **free** web services spin down after ~15 minutes of inactivity, and the
next request then pays a ~30–50s cold start while the container wakes. For an
analytics proxy that's a real problem: the first visitor after an idle period (and
any quick bounce during wake-up) can have their events dropped — exactly the traffic
worth catching.

Fix: a lightweight external health check keeps the instance warm. This project uses
**[UptimeRobot](https://uptimerobot.com/)** (free) hitting `/health`:

| Setting  | Value                                                    |
| -------- | -------------------------------------------------------- |
| Type     | HTTP(s)                                                  |
| URL      | `https://portfolio-backend-2huw.onrender.com/health`     |
| Interval | 5 minutes                                                |

A 5-minute ping resets the idle timer (comfortably under the 15-min sleep) and, as a
bonus, emails on downtime. One always-on service stays within Render's free
750 instance-hours/month.

> A GitHub Actions cron is **not** a good substitute here: its schedule is best-effort
> (often delayed 5–15 min or skipped under load) and it auto-disables after 60 days
> without repo activity — so it can silently let the instance sleep. A dedicated uptime
> monitor is the reliable choice. The proper fix for zero cold starts is a paid
> always-on plan (or a host whose free tier doesn't sleep, e.g. Koyeb).

## Roadmap

- [x] Deployed running server + health check + CI
- [x] PostHog first-party reverse proxy
- [ ] AI assistant chat endpoint (LLM, SSE streaming)
- [ ] Rate limiting + abuse/cost caps
- [ ] Postgres (conversation history) when persistence is needed
