# Infrastructure — domain, DNS, and hosting

How `hirees.me` reaches this Django service, why each piece is configured the way it is,
and what to change when moving to another host.

## The request chain

```
Visitor
  │  looks up hirees.me
  ▼
Spaceship (registrar)          ← where the domain is bought/renewed
  │  delegates DNS via nameservers
  ▼
Cloudflare (DNS, free plan)    ← where records live; also TLS + CDN when proxied
  │  CNAME → portfolio-backend-2huw.onrender.com
  ▼
Render (web service)           ← Django/gunicorn/uvicorn
```

Three separate concerns, deliberately split:

- **Registrar** owns the *registration* (billing, renewal, transfer authorisation).
- **DNS provider** answers "what address is `hirees.me`?" — this is the layer that moves
  when you change hosts, and it's why DNS is at Cloudflare rather than at the registrar.
- **Host** runs the application.

Keeping DNS at Cloudflare rather than Spaceship is the load-bearing choice: switching
cloud provider then means editing one record, with no registrar involvement and no
nameserver propagation wait.

## The full landscape

Who owns what, and which boundaries you cross on one request. Nothing in the naming layer
knows or cares what the hosting layer runs — that separation is what makes the host
replaceable.

```
                        ┌────────────────────────────────┐
                        │           VISITOR              │
                        │   browser → https://hirees.me  │
                        └───────────────┬────────────────┘
                                        │
             ① "what address is hirees.me?"
                                        │
╔═══════════════════════════════════════▼══════════════════════════════════════╗
║  NAMING LAYER — who owns the name, and what it points at                     ║
║                                                                              ║
║   ┌──────────────────────────┐  delegates  ┌─────────────────────────────┐   ║
║   │  SPACESHIP  (registrar)  │  ─────────► │  CLOUDFLARE  (DNS, free)    │   ║
║   │                          │ nameservers │                             │   ║
║   │  • owns the registration │             │  • holds the records        │   ║
║   │  • renewal + billing     │             │    CNAME @    → Render      │   ║
║   │  • transfer auth code    │             │    CNAME www  → Render      │   ║
║   │  • WHOIS contact         │             │  • proxy: OFF (DNS only)    │   ║
║   │                          │             │  • when ON: TLS, CDN, WAF   │   ║
║   └──────────────────────────┘             └──────────────┬──────────────┘   ║
╚═══════════════════════════════════════════════════════════╪══════════════════╝
                                                            │
             ② resolves to portfolio-backend-2huw.onrender.com
                                                            │
╔═══════════════════════════════════════════════════════════▼══════════════════╗
║  HOSTING LAYER — Render (free plan, Frankfurt)                               ║
║                                                                              ║
║   ┌──────────────────────────────────────────────────────────────────────┐   ║
║   │  WEB SERVICE  ·  gunicorn + uvicorn worker (async ASGI), 1 worker    │   ║
║   │                                                                      │   ║
║   │   DJANGO APP (config.asgi)                                           │   ║
║   │   ├── core             /  ·  /health  ·  /favicon.svg                │   ║
║   │   ├── analytics_proxy  /ingest/*   → PostHog EU                      │   ║
║   │   ├── chat             /chat/stream (SSE)  ·  conversations  ·  …    │   ║
║   │   └── admin            /admin/  (Unfold theme — the ops console)     │   ║
║   │                                                                      │   ║
║   │   ALLOWED_HOSTS = .onrender.com,.hirees.me   ← or every request 400s │   ║
║   │   custom domains: hirees.me, www.hirees.me   (cert auto-issued)      │   ║
║   └───────────────────────────────┬──────────────────────────────────────┘   ║
║                                   │                                          ║
║   ┌───────────────────────────────▼──────────────────────────────────────┐   ║
║   │  POSTGRES (free plan)  ·  DATABASE_URL  ·  expires after a window    │   ║
║   │  conversations · messages · facts · documents · chat models · keys   │   ║
║   └──────────────────────────────────────────────────────────────────────┘   ║
╚═════════════════════════════╪════════════════════╪═══════════════════════════╝
                              │                    │
             ③ outbound calls the app makes at request time
                              │                    │
        ┌─────────────────────▼──┐  ┌──────────────▼───────┐  ┌────────────────┐
        │  MISTRAL  (LiteLLM)    │  │  GITHUB API          │  │  POSTHOG EU    │
        │  chat + guard + chips  │  │  repos, READMEs      │  │  analytics     │
        │  EU tier, keys in      │  │  token optional      │  │  via /ingest   │
        │  admin or env          │  │                      │  │  reverse proxy │
        └────────────────────────┘  └──────────────────────┘  └────────────────┘

        ┌────────────────────────────────────────────────────────────────────┐
        │  UPTIMEROBOT (external)  ── GET/HEAD /health every 5 min ──►       │
        │  Keeps the free instance from sleeping (~15 min idle → cold start) │
        └────────────────────────────────────────────────────────────────────┘

        ┌────────────────────────────────────────────────────────────────────┐
        │  GITHUB repo  ── push to main ──►  Render autoDeploy               │
        │  build runs pytest first: a red suite aborts the deploy            │
        └────────────────────────────────────────────────────────────────────┘
```

Read the layers as answering three different questions:

| Layer | Question it answers | Swap cost |
| --- | --- | --- |
| Registrar (Spaceship) | Who legally holds the name? | Transfer, 60-day lock, rarely worth it |
| DNS (Cloudflare) | What does the name point at? | Nameserver change, 1–24h propagation |
| Host (Render) | What runs the code? | **Edit two records — the cheap one** |

The whole point of the arrangement: the expensive-to-change layers know nothing about the
cheap one. Render appears exactly twice in Cloudflare's records, so replacing it is a
two-field edit.

## Current values

The ids below are **identifiers, not credentials** — they're deliberately recorded here so
anyone (or any agent) picking this up knows where things live. They grant nothing on their
own; every API call also needs a token, which lives in Render's environment or Cloudflare's
dashboard and is never committed. Don't try to rotate them.

| Thing | Value |
| --- | --- |
| Domain | `hirees.me` |
| Registrar | Spaceship — $8.70 first year, **$15.53 renewal**, auto-renew on, expires 2027-07-25 |
| DNS | Cloudflare, Free plan |
| Nameservers | `kianchau.ns.cloudflare.com`, `rosa.ns.cloudflare.com` |
| Cloudflare zone id | `5596becd0f0c932bdebd05de9784efa8` |
| Host | Render, service `portfolio-backend` (`srv-d99vs3ecjfls739139ng`), free plan |
| Render hostname | `portfolio-backend-2huw.onrender.com` |

### DNS records

| Type | Name | Target | Proxy |
| --- | --- | --- | --- |
| CNAME | `@` (apex, = `hirees.me`) | `portfolio-backend-2huw.onrender.com` | DNS only |
| CNAME | `www` | `portfolio-backend-2huw.onrender.com` | DNS only |
| CNAME | `api` | `portfolio-backend-2huw.onrender.com` | DNS only |

`api.hirees.me` was added on **2026-07-26** as the backend's public API hostname (social
sign-in callbacks land here — see `docs/auth.md`). It's a Render **Custom Domain**
(Settings → Custom Domains) alongside the apex and `www`; status after setup: **Verified**,
certificate issuing. Kept **DNS only** for the same reason as the others (Gotcha #1).

A `CNAME` at the apex is normally illegal in DNS; Cloudflare allows it via **CNAME
flattening**, which is why no `A` record is needed. If a host ever requires a literal
`A` record, Render's address is `216.24.57.1` — but prefer the CNAME, since an IP can
change under you and a hostname can't.

### Django

`ALLOWED_HOSTS` (Render → Environment) must include the domain or every request returns
**400 Bad Request**:

```
.onrender.com,.hirees.me
```

The leading dot means "this domain and any subdomain", so `souhaib.hirees.me` is covered
without re-editing. `.onrender.com` stays so the Render URL and the UptimeRobot health
check keep working.

## Gotchas that cost real time

**1. Render cannot verify a domain while Cloudflare's proxy is on.** The orange cloud makes
the name resolve to Cloudflare's addresses; Render checks for *its own* hostname and fails
with "We weren't able to verify hirees.me". Set both records to **DNS only** (grey cloud),
verify in Render, and only then consider re-enabling the proxy.

**2. Re-enabling the proxy needs the right TLS mode.** If you turn the orange cloud back on,
set Cloudflare's SSL/TLS mode to **Full (strict)** so the Cloudflare→Render leg stays
encrypted. Left on Flexible, that leg is plain HTTP. Note that a proxied origin can also
interfere with Render's certificate *renewals*, not just the first issue — worth watching
around the 90-day mark.

**3. Nothing works until Cloudflare shows the zone as active.** Records added to a pending
zone are invisible to the world, because the registrar is still pointing at the old
nameservers. Propagation is typically 1–2 hours, occasionally 24.

**4. DNSSEC must be off at the registrar during a nameserver change,** or resolvers reject
the new delegation. Re-enable it through Cloudflare afterwards if wanted.

**5. Certificates are automatic.** Render issues and renews its own; there is no file to
download or install. "Certificate Pending" becomes "Certificate Issued" on its own.

## Changing cloud provider

Only the DNS layer moves. The registrar and Cloudflare stay untouched.

1. Deploy the app on the new host; get its hostname.
2. In Cloudflare DNS, edit the two CNAME targets to the new hostname. Set **DNS only** if
   the new host needs to verify ownership.
3. Add the domain in the new host's dashboard and let it verify + issue a certificate.
4. Update `ALLOWED_HOSTS` on the new host.
5. Lower the records' TTL to 5 minutes a day *before* the switch if downtime matters —
   `Auto` means resolvers may cache the old target for a while.
6. Point the uptime monitor at the new health URL.

Because the domain is not tied to any host, there is no lock-in: no host owns the name, and
no migration requires touching the registrar.

## Free-tier constraints still in force

- Render free sleeps after ~15 minutes idle (~30–50s cold start). An external UptimeRobot
  check on `/health` every 5 minutes keeps it warm. `/health` answers both GET and HEAD.
- One async worker only — the LiteLLM + LangGraph stack cannot fit twice in 512MB. See
  `render.yaml`.
- Render's free Postgres expires after a limited window. The app reads `DATABASE_URL`, so
  moving to Neon or Supabase is a one-variable change.

## Planned: wildcard subdomains per tenant

The multi-tenant direction (`souhaib.hirees.me`, `someone.hirees.me`) needs:

- A Cloudflare `CNAME` record named `*` pointing at the host — one record covers every
  tenant, so signup requires no DNS work.
- A **wildcard custom domain** (`*.hirees.me`) registered at the host. Verify the host
  supports this; if not, each tenant subdomain has to be registered through the host's API
  at signup.
- Cloudflare's free Universal SSL already covers `*.hirees.me` at one level deep, which is
  exactly this case. Deeper nesting (`a.b.hirees.me`) would need a paid certificate.
- `ALLOWED_HOSTS` already covers it via the leading dot on `.hirees.me`.
- Reserve `www`, `app`, `api`, `admin`, `mail`, `status`, `docs` before a user can claim
  one as their slug.
- Do **not** set the session cookie domain to `.hirees.me`. A parent-domain cookie is sent
  to every tenant subdomain, so one cross-site scripting hole on any tenant page would leak
  sessions platform-wide. Scope it to the dashboard host only.
