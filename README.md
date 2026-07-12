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
| ANY    | `/ingest/<path>`  | Reverse proxy to PostHog (see below)                           |

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

## Deployment (Render)

This repo ships a [`render.yaml`](./render.yaml) Blueprint.

1. Push to GitHub.
2. In Render → **New + → Blueprint** → select this repo → **Apply**.
3. Render provisions the service, generates `SECRET_KEY`, and deploys.

`autoDeploy` is on, so every push to `main` redeploys.

## Roadmap

- [x] Deployed running server + health check + CI
- [x] PostHog first-party reverse proxy
- [ ] AI assistant chat endpoint (LLM, SSE streaming)
- [ ] Rate limiting + abuse/cost caps
- [ ] Postgres (conversation history) when persistence is needed
