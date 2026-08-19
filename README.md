# VOX / ORCHESTRATOR

A voice interface for agentic workflows where **you can interrupt the agent mid-action** — *"wait no, change that to Tuesday"* — and it cancels the tool call it was running, figures out what already happened, undoes it if needed, and re-plans around it. [n8n](https://n8n.io) workflows are the agent's "hands."

Most voice agents make you wait for them to finish before you can correct them. This one lets you cut in the way you would with a person — and, crucially, it does the right thing even when it was **halfway through a side effect** (booking a meeting, sending a request) at the moment you spoke.

---

## The problem this solves

Barging in mid-*sentence* is easy: you just stop the audio. Barging in mid-*action* is the hard part, because a tool call may have already changed the world by the time the user interrupts.

Say the agent is booking a Monday meeting and you cut in with *"make it Tuesday."* Three things can be true at that instant:

- the request to create the Monday event **never left** → nothing happened, just book Tuesday
- the request **completed** → a real Monday event exists → delete it, then book Tuesday
- the request was **in flight** → you genuinely don't know → treat it as *maybe live* and reconcile it

A naive agent ignores this and just books Tuesday; leaving a phantom Monday event on your calendar. This project treats that third case as a first-class citizen.

---

## What makes it correct

Three ideas do the heavy lifting:

**1. A turn is one cancellable task.**
Planning, speaking, and running tools all execute inside a single `asyncio` task. A barge-in cancels that task, which unwinds every in-flight `await` — including the HTTP request to n8n.

**2. Cancelled ≠ didn't happen.**
When a tool is cancelled, the executor records *when*:

| Situation | Status | Meaning |
|-----------|--------|---------|
| cancelled **before** the request left the machine | `CANCELLED` | nothing happened |
| cancelled **while in flight** | `UNCERTAIN` | n8n may have executed it |

It never pretends an uncertain effect is clean.

**3. A pending-effects ledger, persisted across chained interruptions.**
Every effect that might still be live — completed creates, plus interrupted calls marked `UNCERTAIN` — goes into a ledger. The re-planner reconciles **all** of them, not just the last one. So a rapid *Monday → Tuesday → Wednesday* still deletes the earlier still-uncertain events before booking Wednesday, instead of leaving duplicates behind.

`send_email` is treated as terminal (you can't un-send), so the planner confirms intent before firing it rather than relying on a compensation.

---

## See it working

Here's a real run. The user schedules Monday, then barges in mid-call:

```
plan ready — 3 step(s)
"Sure, scheduling Meeting for Monday at 10am."
→ create_calendar_event(title: Meeting, day: Monday, time: 10am)
⚡ barge-in: "wait no, change that to Tuesday"
✓ create_calendar_event — uncertain          ← caught mid-flight, not assumed clean
re-planned — 4 step(s)
"Got it — moving it to Tuesday."
→ delete_calendar_event(title: Meeting, day: Monday)   ← undo the maybe-live event
✓ delete_calendar_event — completed
→ create_calendar_event(title: Meeting, day: Tuesday, time: 10am)
✓ create_calendar_event — completed
"Done — Meeting is now on Tuesday."
— turn complete —
```

The `uncertain → delete → rebook` sequence is the whole point: the agent reconciled an effect it couldn't be sure about, instead of leaving a phantom on the calendar.

---

## Architecture

```
 browser (voice + telemetry UI)
        │  WebSocket   (utterances, barge-in, cancel  ▲   state / plan / tool events ▼)
        ▼
 FastAPI  ──  Orchestrator (async state machine)
                 ├── Planner       provider-agnostic: mock | anthropic | openai | deepseek
                 └── ToolExecutor   → n8n webhooks (or mock mode)
                                     carries Idempotency-Key; tracks CANCELLED vs UNCERTAIN
```

**State machine:** `IDLE → PLANNING → SPEAKING → EXECUTING`, with `INTERRUPTED → REPLANNING` splicing in whenever the user barges. A single `asyncio.Lock` guards the transitions so a barge-in and a finishing tool can't race.

### Backend (`backend/`)

| File | Role |
|------|------|
| `orchestrator.py` | The core. Turn-as-task, barge-in handling, the ledger, reconciliation. |
| `planner.py` | Turns utterances into JSON plans. Real LLMs + a rule-based mock planner. |
| `tools.py` | Runs tools against n8n; the `CANCELLED`-vs-`UNCERTAIN` honesty lives here. |
| `models.py` | Pydantic models + the WebSocket wire protocol. |
| `main.py` | FastAPI app: serves the UI, `/health`, and the `/ws` endpoint. |

### Frontend (`frontend/`)

A dark "signal monitor" UI, no build step — plain `index.html` / `styles.css` / `app.js`:

- a live **oscilloscope** whose amplitude and color track the agent's state (teal = calm, coral = interruption)
- the **current plan** rendered step-by-step, with `ACTION` / `UNDO` / `SAY` tags
- an **event log** that shows the plan being torn down and rebuilt the instant you barge in

### The tool catalogue

| Tool | Args | Reversible | Compensated by |
|------|------|:----------:|----------------|
| `create_calendar_event` | `title, day, time` | ✅ | `delete_calendar_event` |
| `delete_calendar_event` | `title, day` | — | — |
| `add_task` | `title, due` | ✅ | `remove_task` |
| `remove_task` | `title` | — | — |
| `send_email` | `to, subject, body` | ❌ *irreversible* | — *(confirm before firing)* |
| `search_web` | `query` | read-only | — |

---

## Quickstart

Requires **Python 3.11+**. Runs fully offline in mock mode; no API keys, no n8n.

```bash
# 1. install
cd backend
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. run  (from inside backend/ — the app serves the frontend by relative path)
python -m uvicorn main:app --port 8000
```

Open **http://localhost:8000**. When it starts cleanly the terminal prints `Uvicorn running on http://0.0.0.0:8000`.

### Try the barge-in (the part worth seeing)

The interrupt only means something *mid-turn*, so give yourself a wide window to click into:

```bash
MOCK_TOOL_LATENCY=4 SPEAK_SECONDS_PER_WORD=0.5 python -m uvicorn main:app --port 8010
```

1. Click **Schedule Monday**.
2. While the state still reads **speaking** or **executing** (before *"— turn complete —"*), click **⚡ "change to Tuesday"**.
3. Watch the log mark the Monday event `uncertain`, delete it, and rebook Tuesday.

Drop the timing overrides once you've seen it. You can also drive everything by typing into the console (works in every browser, including Firefox) — an instruction sent while the agent is busy is automatically treated as a barge-in.

---

## Configuration

Everything is optional. With no `.env` at all, the system runs mock planner + mock tools. Copy `.env.example` to `.env` (in the project root) to turn on real pieces independently.

| Variable | Default | What it does |
|----------|---------|--------------|
| `LLM_PROVIDER` | `mock` | `mock` \| `anthropic` \| `openai` \| `deepseek` |
| `LLM_API_KEY` | — | key for the chosen provider; blank → silently falls back to mock |
| `LLM_MODEL` | provider default | e.g. `claude-sonnet-4-6`, `gpt-4o`, `deepseek-chat` |
| `N8N_BASE_URL` | — | blank → mock tools; set it to fire real webhooks |
| `N8N_WEBHOOK_PREFIX` | `/webhook` | tools are called at `{N8N_BASE_URL}{PREFIX}/{tool_name}` |
| `MOCK_TOOL_LATENCY` | `2.5` | simulated tool latency (s); higher = easier to barge in mid-call |
| `SPEAK_SECONDS_PER_WORD` | `0.30` | how long the agent "speaks" a line |
| `SPEAK_SECONDS_CAP` | `6.0` | cap on speaking time per line |

The layers are independent: real LLM + mock tools, or mock planner + real n8n, both work.

---

## Connecting n8n

You **don't** need n8n to demo the barge-in mock tools simulate latency and behave identically. Wire it up only when you want tools to actually touch a real calendar / inbox.

Each tool maps to one n8n **Webhook** workflow. The executor sends a JSON `POST` with an **`Idempotency-Key`** header to honor it. When a call is cancelled mid-flight and marked `UNCERTAIN`, the re-planner may fire the same call again or its compensation; idempotency on that key is what keeps interruption recovery from double-booking.

Full setup, per-workflow request shapes, and an idempotency pattern are in [`n8n/example-workflows.md`](n8n/example-workflows.md).

---

## Testing

```bash
cd backend
export MOCK_TOOL_LATENCY=1.0 SPEAK_SECONDS_PER_WORD=0.02 SPEAK_SECONDS_CAP=0.2

python test_orchestrator.py   # canonical: Monday → barge mid-call → Tuesday
python test_robustness.py     # "cancel that" undo; rapid double barge-in (→ Wednesday)
python test_ws.py             # end-to-end over a live WebSocket (start the server first)
```

`test_orchestrator.py` and `test_robustness.py` assert the ledger actually marks the interrupted Monday create as `uncertain` and that the re-plan deletes it before rebooking — i.e. that interruption recovery is real, not cosmetic.

---

## Design notes & limitations

- **Hold-to-talk, by design.** In a browser tab the agent's speech and your mic share one acoustic space with no echo cancellation, so an always-listening mic would hear the agent and interrupt itself. Holding to speak makes barge-in intent explicit and sidesteps the echo problem and pressing the mic *while the agent is talking* **is** the barge-in.
- **Instant audio stop.** On speech-start, local TTS is killed immediately (no server round-trip) so the interruption *feels* instant; the actual re-plan is sent once the final transcript is ready.
- **Browser STT varies.** Web Speech recognition isn't everywhere (e.g. Firefox). The text console is a first-class fallback that drives the exact same barge-in path.
- **Scope.** The planner ships with a small demo tool catalogue and a rule-based mock planner so the whole system runs offline; swapping in a real LLM provider is a config change, not a code change.

---

## Tech stack

- Python 
- FastAPI 
- asyncio 
- WebSockets 
- Pydantic 
- httpx 
- vanilla JS/CSS frontend 
- n8n (pluggable backend) 
- Anthropic / OpenAI / DeepSeek (pluggable planner)

---

## License

[MIT](LICENSE) © 2026 Pranjali Srivastava