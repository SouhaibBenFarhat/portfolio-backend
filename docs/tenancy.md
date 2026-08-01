# Tenancy — who owns what, and how a request finds them

Every document, fact and conversation belongs to a **tenant**: a signed-in user with a
`core.models.Profile`. This is what took the backend from "Souhaib's portfolio with a chat
on it" to something a second person can sign up to. Sign-in itself is in `docs/auth.md`;
domain and DNS are in `docs/infrastructure.md`.

## The model

| Thing | Where | Note |
| --- | --- | --- |
| `Profile` | `core/models.py` | 1:1 with `User`. Holds `handle`, `github_username`, `is_published`. |
| `owner` FK | `chat.Document`, `chat.Fact`, `chat.Conversation` | Mandatory. `on_delete=CASCADE`. |
| Platform-owned, deliberately | `LLMCredential`, `ChatModel`, `RequestLog`, `TokenUsage` | Keys and the model chain are the operator's, not a tenant's. |

`Profile` is a side table rather than a custom user model because `AUTH_USER_MODEL` can only
be swapped on an empty database, and this one already had accounts, allauth rows and social
links pointing at the stock user.

**`handle` is a DNS label**, not a slug — lowercase letters, digits and hyphens, starting and
ending alphanumeric, 63 characters max. A `SlugField` would have allowed underscores and
uppercase, both illegal in a hostname, so a handle that saved fine would have produced a
subdomain that could never resolve. `RESERVED_HANDLES` in `core/models.py` blocks the names
the platform needs (`www`, `app`, `api`, `admin`, `mail`, `docs`, …) — and the reserved list
is checked again at *resolution* time, so a name that becomes reserved after someone claimed
it stops serving rather than quietly keeping its page.

**`Document.slug` is unique per owner, not globally.** It was globally unique when this was
one person's CV; the second tenant to upload one would have collided on `slug="cv"` and been
unable to save.

## How a request finds its tenant

`core/tenancy.py`, in this order:

1. **The subdomain.** `souhaib.hirees.me` → tenant `souhaib`. The host is the one part of a
   request a caller can't lie about without also failing `ALLOWED_HOSTS`, so it beats
   anything in the body — a visitor on one tenant's page must not be able to ask about
   another by editing a JSON field.
2. **An explicit `handle`** in the request body, for callers with no subdomain: local
   development, and previewing a page before it is published.
3. **`FALLBACK_TENANT_HANDLE`.** The Astro portfolio posts to the apex host and names no
   tenant at all, because it was written when there was only one. Without this step,
   ownership would have broken that chat on the day it shipped.

Only **published** profiles resolve. Nothing resolvable is a 404, never a guess: guessing
means answering out of whichever data happened to be found.

## How the agent is kept to one tenant

The owner's id travels in the **LangGraph run config**
(`config={"configurable": {"owner_id": ...}}`), set by the view from the resolved request.
LangChain injects that `config` into every tool and **keeps it out of the schema the model is
shown** — so the model cannot read the owner, cannot pass one, and cannot ask for a different
one. `test_the_owner_is_hidden_from_the_schema_the_model_sees` pins exactly that.

Two alternatives were rejected:

- **Binding the owner into the tools when the agent is built.** That means one cached agent
  per tenant, and `chat/agent.py` caches by `(model, key)` precisely so the 512 MB instance
  holds one set.
- **Letting the tenant be a tool argument.** Then the model chooses, and a prompt injection
  in an uploaded CV chooses with it.

A tool with no owner in its config returns "nothing found" rather than running unscoped. The
failure mode of the other choice is one tenant's CV being read out on another's page.

The tenant's **name** reaches the model separately, as a system line ahead of the thread
(`chat.agent.tenant_preamble`), for the same cache reason. It is prepended *after* the
history is trimmed, so a long conversation can't push it out. A wrong name there is cosmetic;
the data the model can reach is fixed by the run config, not by the prose.

## Migrating an existing instance

Three migrations, in order:

1. `chat/0013_…` adds the nullable `owner` columns and swaps `Document.slug`'s global
   uniqueness for `unique_document_per_owner`.
2. `core/0005_backfill_tenant_one` creates a profile for the **earliest staff account** and
   assigns every existing row to it, published. Staff rather than superuser, and rather
   than any account: every document and fact here was created through `/admin`, and
   `/admin` requires `is_staff` — so the account that manages the content is the one that
   owns it, and a visitor who signed in through the public flow can never be picked.
   It is a guess and it can miss — a setup admin made early enough has a lower id than the
   real owner. An explicit `DEFAULT_TENANT_EMAIL` setting was written and then dropped as
   more ceremony than the problem deserves: this runs **once**, on a database whose accounts
   you can read beforehand. If it does pick wrong the fix is a reassignment, not a rollback —
   repoint `owner_id` on the three tables and move the `Profile` with it.
   The handle comes from `settings.DEFAULT_TENANT_HANDLE`, **not** a literal, on the same
   reasoning as `chat/migrations/0010_seed_chat_models`: another instance running this code
   has a different first account, and a migration that assumed ours would hand them a
   stranger's name.
3. `chat/0014_require_owner` makes the columns `NOT NULL`. Hand-written, because
   `makemigrations` can only offer to invent a default for rows that 0005 has already filled.

On a fresh database every step is a no-op: there is no user, so there is nothing to own and
nobody to own it, and the `NOT NULL` applies to empty tables. **A brand-new deployment
therefore has no tenants and `/chat/stream` 404s until somebody signs up** — which is correct,
and is why `chat/tests.py` creates a tenant explicitly via `_tenant()` instead of relying on
a fixture.

## Settings

| Setting | Default | What it does |
| --- | --- | --- |
| `TENANT_BASE_DOMAIN` | `hirees.me` | The domain tenant subdomains hang off. |
| `DEFAULT_TENANT_HANDLE` | `souhaib` | The handle given to tenant #1 by the backfill. |
| `FALLBACK_TENANT_HANDLE` | = `DEFAULT_TENANT_HANDLE` | Who a request naming no tenant is answered as. **Set it empty** once every visitor arrives through a tenant's own subdomain, and an unresolvable request becomes a 404. |

## Still to do

- **Public tenant pages (Phase 3).** Resolution works; nothing renders a page yet.
  `*.hirees.me` wildcard DNS is planned but not set up — see `docs/infrastructure.md`.
- **Per-tenant rate limits and token quotas.** Today's limits are per-IP and per-conversation,
  so one tenant's traffic still spends the shared free tier.
- **`GITHUB_USERNAME`** remains as the setting for nothing but historical defaults; the tools
  read `Profile.github_username`.
