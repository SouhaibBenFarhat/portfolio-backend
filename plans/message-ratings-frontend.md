# Message ratings — frontend plan

Handoff for the portfolio repo (`souhaibbenfarhat.github.io`). The backend side ships in
the `message-ratings` branch of `portfolio-backend`.

## Why

Each reply should be judgeable — a 👍/👎 on every message. Ratings are stored per message
and summed per conversation in the admin, so real feedback can be reviewed.

## What the backend gives you

**Rating endpoint** (DRF, documented — types come from the openapi sync):

```
PUT /chat/conversations/<uuid>/messages/<id>/rating/    { "rating": 1 | -1 | 0 }
→ 200  { "id": <id>, "rating": 1 | -1 | null }
→ 404  unknown conversation, or the message isn't in it
→ 400  rating outside -1..1
```

Idempotent — it *sets* the value. `1` up, `-1` down, `0` clears back to null. To toggle a
thumb off, send `0`.

**A message's id, two ways** (you need it to call the endpoint):

- **Restore** — `GET /chat/conversations/<uuid>/` now returns each message with `id` and
  `rating` (alongside `role`/`content`), so a reloaded thread shows the thumbs already set.
- **Live** — the stream emits a `message_id` frame naming the just-persisted reply, after
  the answer text:

  ```json
  {"message_id": 1234}
  ```

  Absent when the turn broke (nothing was persisted, nothing to rate). The frame order is
  now: `conversation_id` → `model` → `text`/`tool` → `message_id` → `usage` →
  `suggestions` → `done`.

## Work

All in `src/components/Chat/ChatWidget.tsx` unless noted.

1. **Message identity.** Give each rendered message an `id` and `rating`. On restore, read
   them from the payload. On a live turn, capture the `message_id` frame and attach it to
   the assistant message just rendered.
2. **Parse the frame.** Add a `message_id` case to the SSE reader → set it on the current
   assistant message.
3. **Thumb buttons.** Render 👍/👎 on each message (assistant replies at least; user
   messages are optional and arguably pointless). Reflect the current `rating`: highlight
   the active thumb.
4. **Send on tap.** Tapping a thumb calls the endpoint with `1`/`-1`; tapping the active
   thumb again sends `0` (toggle off). Optimistically update, roll back on non-200.
5. **A message with no id yet** (mid-stream, before the `message_id` frame) shouldn't show
   active buttons — disable them until the id lands.

## Edge cases

- **No `message_id` frame** on a broken turn — leave that message unratable (there's no row
  to rate).
- **Restore 404** (free DB reset) already resets the thread; ratings reset with it.
- **Toggle semantics live in the frontend** — the API only ever sets an absolute value.

## Design

Match the site's minimalist tokens — quiet, small, low-contrast thumbs that sit at the edge
of a message and only gain color when active. No animated fills, no gradients. They're
secondary to the message text, not competing with it.

## Testing

`vitest` + `@testing-library/react`: a tap sets the rating (mocked fetch called with the
right body), the active thumb reflects `rating`, tapping it again sends `0`, restore shows
persisted thumbs, and a non-200 rolls back the optimistic state.
