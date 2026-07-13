# AI Chat Assistant — Implementation Plan

A streaming, agentic chat assistant for the portfolio. Goal: showcase full
understanding of the modern AI stack to recruiters — real tool calling, visible
agent steps, multi-provider failover, persistence, smooth streaming.

## Vision

A recruiter opens the portfolio, chats with an assistant that can:
- Answer questions about Souhaib (CV, salary expectations, availability, hobbies).
- Explore GitHub live — list projects, read READMEs, answer questions about the code.
- Show **real** agent activity as premium animations ("loading facts…", "exploring
  projects…", "baking the response…") driven by actual tool events.
- Never break, even when a free model runs out of quota (automatic provider switch).

## How the pieces fit

```
Browser (portfolio frontend)
   │  POST /chat  { message, conversation_id }   ← Server-Sent Events stream back
   ▼
Django (async endpoint)
   │
   ▼
LangGraph agent  ──────────────► holds conversation memory (Postgres checkpointer)
   │  decides tool calls, emits step events
   ├─ Tool: get_facts(category?)        → Fact model (DB)
   ├─ Tool: get_cv()                    → Document model (DB)
   ├─ Tool: list_github_projects()      → GitHub API
   └─ Tool: get_repo_readme(repo)       → GitHub API
   │
   ▼
LiteLLM (model layer)  ──────────► Gemini (free) → Groq/Llama (free) failover
```

- **LangGraph** = runs the agent + conversation memory.
- **LiteLLM** = the swappable model engine underneath (provider failover).
- **Vercel AI SDK** = frontend; renders the stream + tool-step animations.
- Tools are plain functions (DB queries, GitHub API) — a **single** LLM orchestrates
  them. Not one LLM per tool.

## Tech decisions

| Concern | Choice |
| --- | --- |
| Model gateway | **LiteLLM** (library, in-backend, free) |
| Primary model | **Gemini** free tier (supports tool calling) |
| Fallback model | **Groq / Llama 3.3 70B** free tier (supports tool calling) |
| Conversation memory | **Django `Conversation`/`Message` models** in Postgres (testable in CI with SQLite; visible in admin) |
| Agent runtime | **LangGraph** — introduced in Phase 3, where tools need it |
| Persistence DB | **Postgres** (Render free tier) — SQLite won't survive Render's ephemeral disk |
| Server mode | **Async** (`gunicorn config.asgi:application -k uvicorn.workers.UvicornWorker --timeout 0`) |
| Failover granularity | **Per turn** (switch engine between messages, never mid-stream) |
| Frontend transport | **Server-Sent Events**, in Vercel AI SDK stream format |
| Facts management | **Django admin** UI (edit facts without redeploy) |

## New Django app: `chat`

Holds the models, the async endpoint, the tools, and the LangGraph agent.

### Models

```python
# Phase 2 — conversation persistence
class Conversation(models.Model):
    id = UUIDField(primary_key, default=uuid4)   # anonymous, unguessable session id
    created_at, updated_at

class Message(models.Model):
    conversation = ForeignKey(Conversation, related_name="messages")
    role    = CharField          # "user" | "assistant"
    content = TextField
    created_at

# Phase 3 — knowledge base (edited in Django admin)
class Fact(models.Model):          # short recruiter Q&A
    category, question, answer, is_active, order

class Document(models.Model):      # long-form content (CV, bio)
    slug, title, content, is_active

# Admin-managed provider API keys (encrypted at rest)
class LLMCredential(models.Model):  # multiple keys per provider, tried in order
    provider, label, api_key (encrypted), is_active
```

**Admin-managed API keys:** LLM provider keys live in `LLMCredential` (edited in the
admin), so they can be added/rotated without touching env vars. The `api_key` is
Fernet-encrypted at rest (key derived from `SECRET_KEY` — no extra env var); the DB
only holds ciphertext. Multiple keys per provider are allowed and tried in order;
a provider with no admin key falls back to its env var. Agents are cached by the
set of keys in use, so a rotation takes effect on the next request.

Conversation history lives in the `Conversation`/`Message` models, keyed by an
anonymous `conversation_id` (a UUID). Chosen over LangGraph's Postgres checkpointer
because it stays inside Django's migrations, is testable in CI without a live Postgres,
and is visible in the admin. LangGraph (Phase 3) reads this history rather than owning
its own persistence.

### Tools

| Tool | Reads | Animation label |
| --- | --- | --- |
| `get_facts(category?)` | Fact model | "loading facts…" |
| `get_cv()` | Document model (slug="cv") | "reading the CV…" |
| `list_github_projects()` | GitHub API | "exploring projects…" |
| `get_repo_readme(repo)` | GitHub API | final answer → "baking the response…" |

Labels are **real** — driven by which tool actually fires (streamed from LangGraph),
not scripted.

## Request flow

1. Browser sends `POST /chat` with `{ message, conversation_id }`.
2. Async Django view runs a **rate-limit** check (per IP/session).
3. LangGraph agent runs with `thread_id = conversation_id`, loading prior state from Postgres.
4. Agent (LLM via LiteLLM) decides tool calls; tools execute; LangGraph emits step events.
5. Events are translated to the **Vercel AI SDK** stream format and sent as Server-Sent Events.
6. Frontend renders the tool-step animations + streamed answer.
7. New state is saved to Postgres by the checkpointer.

## Cross-cutting concerns

- **Anonymous sessions** — no login. A `conversation_id` (UUID) is generated and stored
  in the browser's localStorage; passed on each request.
- **Rate limiting** — public endpoint spending free quotas; throttle per IP/session to
  stop bots draining tokens.
- **Guardrails** — system prompt defines persona + scope (recruiter assistant); cap
  message length; basic prompt-injection caution.
- **Format bridge** — LangGraph event stream → Vercel AI SDK protocol via
  `py-ai-datastream` (or a small manual adapter).
- **Secrets** — `GEMINI_API_KEY`, `GROQ_API_KEY`, `GITHUB_TOKEN`, `DATABASE_URL` set in
  Render's environment (runtime), listed by name in `.env.example`.

## Phased build

- **Phase 0 — Infrastructure**
  - Create the `chat` app; confirm Django admin is enabled.
  - Add Postgres (`DATABASE_URL`) + switch settings to it.
  - Flip the server to async (asgi/uvicorn) in `render.yaml`; keep sync endpoints working.
- **Phase 1 — Minimal streaming chat**
  - One model via LiteLLM, async Server-Sent Events endpoint, a basic test page.
  - No tools, no persistence — just prove the streaming pipe end to end.
- **Phase 2 — Persistence**
  - `Conversation`/`Message` models; anonymous conversation IDs; load history each turn
    and save the exchange so context survives messages and restarts.
- **Phase 3a — Introduce LangGraph** ✅
  - Replace the direct LiteLLM call with a LangGraph agent (model + system persona,
    no tools yet), reading the stored history. Streaming + persistence unchanged.
- **Phase 3b — Knowledge base + admin** ✅
  - `Fact`/`Document` models; enable Django admin (login-protected editing UI);
    Conversations viewable read-only.
- **Phase 3c — Tools + step events** ✅
  - `get_facts`/`get_cv` (async ORM); GitHub tools (list projects, read READMEs).
  - Wire tools into the agent; stream real `on_tool_start`/`on_tool_end` events as
    SSE `tool` frames for the frontend animations.
- **Phase 4 — Multi-model failover** ✅
  - Per-turn failover: one agent per model (Groq → Gemini). If the primary fails
    before any text streams, retry the next; never switch models mid-answer.
- **Phase 5 — Guardrails + rate limiting (backend)** ✅
  - Per-IP rate limit (RequestLog, pruned each request); message-length cap;
    history bounded to the most recent N messages; system-prompt guardrail
    (on-topic + basic prompt-injection resistance).
- **Phase 6 — Frontend**
  - Astro React island + Vercel AI SDK wired to `/chat/stream`, with the real
    tool-event animations. (Lives in the separate portfolio repo.)

## Frontend integration (resolved)

The portfolio is **Astro + Tailwind + TypeScript** (static site generator). Astro
supports **React islands** via `@astrojs/react`, so:

- The site stays static Astro (unchanged, fast).
- Add **one React island** — just the chat widget — hosting the Vercel AI SDK
  `useChat` hook. TypeScript already in place, so it fits naturally.
- No rewrite of the portfolio required.

## Open decisions / risks

- **CV storage** — planned as a `Document` model (admin-editable). Confirm.
- **Free-tier limits** — rate limits + the free server's RAM/CPU cap concurrent streams;
  fine for a portfolio, but worth load-awareness.
- **Optional later** — a `Message` log table for admin visibility; multi-agent
  orchestration as a "wow" extension.
```
