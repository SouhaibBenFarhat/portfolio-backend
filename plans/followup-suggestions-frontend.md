# Follow-up chips — frontend plan

Handoff for the portfolio repo (`souhaibbenfarhat.github.io`). The backend side ships in
the `followup-suggestions` branch of `portfolio-backend`.

## Why

A recruiter doesn't know what the assistant can answer. After each reply the backend now
streams up to 3 follow-up questions; the widget renders them as tappable chips so the next
question is one tap away.

## What the backend gives you

**A `suggestions` frame**, once per turn, after the `usage` frame and just before `done`:

```json
{"suggestions": ["What projects has he built?", "Is he available right now?"]}
```

- Up to 3 short questions, in the visitor's voice — ready to send verbatim as the next
  message.
- **The frame can be absent.** It's omitted when no model actually answered (an error or
  the canned fallback), the thread spent its budget, generation failed or timed out, or
  `CHAT_SUGGESTIONS_ENABLED` is off. Chips simply don't appear that turn — don't reserve
  space for them.
- Generated types: `components['schemas']['ChatSuggestionsFrame']` joins the
  `ChatStreamFrame` union (auto-PR'd by the usual openapi-updated dispatch on merge).

## Work

All in `src/components/Chat/ChatWidget.tsx` unless noted.

1. **Parse the frame.** The SSE reader switches on `text` / `tool` / `model` / `usage` /
   `done`. Add a `suggestions` case → `setSuggestions(data.suggestions)`.
2. **State.** One `suggestions: string[]`, cleared when a new send starts (stale chips
   from the previous turn must not linger next to a new answer).
3. **Chips UI.** Render under the finished reply, above the composer. Tapping a chip
   sends its text as the next message (same path as typing + send) and clears the chips.
4. **Clear on terminal states.** On `error`, on the exhausted gauge, and on "new chat",
   drop the chips — every path that disables or resets the composer should also clear
   them.

## Edge cases

- **Chips arrive before `done`** but after the full answer text — safe to render as soon
  as the frame lands.
- **A turn with no frame keeps no chips** — clear at send-time, so a missing frame means
  no chips rather than last turn's.
- **Restore does not return suggestions.** After a reload there are no chips until the
  next reply — that's fine, don't fake them.

## Design

Match the site's minimalist tokens — quiet outlined chips, no gradients, no shimmer. They
should read as shortcuts, not as content: visually lighter than the assistant's text.
Wrap to at most two lines; let them truncate rather than dominate the widget.

## Testing

`vitest` + `@testing-library/react`: the frame renders chips, tapping one sends its text
and clears the row, a new send clears stale chips, and a stream with no frame renders no
chips.
