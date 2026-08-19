# Wiring the orchestrator to n8n

The orchestrator treats n8n as its **hands**: each tool in the catalogue maps
to one n8n **Webhook** workflow. When `N8N_BASE_URL` is set, the executor
POSTs to:

```
{N8N_BASE_URL}{N8N_WEBHOOK_PREFIX}/{tool_name}
```

So with `N8N_BASE_URL=https://n8n.example.com` and the default prefix
`/webhook`, the `create_calendar_event` tool calls:

```
POST https://n8n.example.com/webhook/create_calendar_event
```

Leave `N8N_BASE_URL` blank and every tool runs in mock mode instead — useful
for developing the voice loop without touching real services.

---

## The request the executor sends

Every call is a JSON POST with two headers that matter:

| Header             | Meaning                                                        |
|--------------------|---------------------------------------------------------------|
| `Content-Type`     | `application/json`                                             |
| `Idempotency-Key`  | Stable per tool call. **Honor it** (see below).               |

Body = the tool's arguments, verbatim. Example for `create_calendar_event`:

```json
{ "title": "Meeting", "day": "Tuesday", "time": "10am" }
```

Your workflow should return a JSON object; whatever you return becomes the
tool's `result` and is surfaced in the event log. A small, meaningful payload
is ideal, e.g.:

```json
{ "event_id": "evt_b3adb0", "title": "Meeting", "day": "Tuesday", "time": "10am" }
```

---

## Why `Idempotency-Key` is not optional here

This is the whole point of the project. When the user barges in **while a tool
call is in flight**, the orchestrator cancels the HTTP request — but it cannot
know whether n8n already executed it. It records that effect as `UNCERTAIN`
and the re-planner reconciles it (often by firing the tool's compensation,
e.g. `delete_calendar_event`).

If your workflow is idempotent on `Idempotency-Key`, a retried or
raced-then-recovered call can't double-book. Practical pattern in n8n:

1. **Webhook** node receives the call; read `{{$json.headers["idempotency-key"]}}`.
2. **Lookup** node checks a store (n8n Data Store, a DB, a Google Sheet, Redis)
   for that key.
3. If seen → return the stored result, do nothing else.
4. If new → do the real work, store `{key → result}`, return the result.

Keep keys for at least as long as a conversation could plausibly run.

---

## The six workflows to create

Each tool is one Webhook workflow. Reversible tools have a **compensation**
tool the re-planner uses to undo a still-live effect after an interruption.

| Workflow (webhook path)   | Body args                | Reversible | Compensated by         |
|---------------------------|--------------------------|------------|------------------------|
| `create_calendar_event`   | `title, day, time`       | ✅         | `delete_calendar_event`|
| `delete_calendar_event`   | `title, day`             | —          | —                      |
| `add_task`                | `title, due`             | ✅         | `remove_task`          |
| `remove_task`             | `title`                  | —          | —                      |
| `send_email`              | `to, subject, body`      | ❌ irrevers.| — (confirm before firing)|
| `search_web`              | `query`                  | read-only  | —                      |

Notes:
- **`send_email` can't be undone.** The planner is instructed to confirm intent
  before firing it rather than rely on a compensation. Treat it as terminal.
- **`search_web` is read-only** — nothing to reconcile if interrupted.
- The compensations (`delete_calendar_event`, `remove_task`) are themselves
  ordinary workflows in the table above; the re-planner just schedules them.

---

## Minimal example: `create_calendar_event`

A Google Calendar version, in four nodes:

1. **Webhook** — method `POST`, path `create_calendar_event`, respond "When Last
   Node Finishes."
2. **Function / IF (idempotency guard)** — look up
   `{{$json.headers["idempotency-key"]}}`; short-circuit if already processed.
3. **Google Calendar → Create Event** — map:
   - Summary ← `{{$json.body.title}}`
   - Start ← derived from `{{$json.body.day}}` + `{{$json.body.time}}`
4. **Set / Respond to Webhook** — return
   `{ "event_id": "{{$json.id}}", "title": "...", "day": "...", "time": "..." }`
   and record the idempotency key alongside it.

Clone this shape for the other five, swapping the middle action node
(Gmail for `send_email`, your task app for `add_task`/`remove_task`, an HTTP/
search node for `search_web`).

---

## Sanity check

With workflows deployed and `.env` pointing at your n8n host:

```bash
curl -X POST "$N8N_BASE_URL/webhook/create_calendar_event" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: test-123" \
  -d '{"title":"Meeting","day":"Tuesday","time":"10am"}'
```

Run it twice with the same key — the second call should return the **same**
result without creating a second event. If it does, interruption recovery will
be safe.
