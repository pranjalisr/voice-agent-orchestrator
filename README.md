# VOX / ORCHESTRATOR

A voice interface for agentic workflows where **you can interrupt the agent
mid-action** — "wait no, make it Tuesday" — and it gracefully cancels the tool
call it was running, re-plans around what already happened, and resumes. n8n
workflows are the agent's "hands."

The interruption → cancel → reconcile → resume loop is the whole point. Most
voice agents make you wait for them to finish before you can correct them. This
one lets you cut in the way you would with a person, and it does the right thing
even when it was **halfway through a side effect** when you spoke.

---

## Why this is hard (and what makes it correct)

Barging in mid-sentence is easy — you just stop the audio. Barging in
**mid-action** is the hard part, because a tool call may have already changed
the world by the time you interrupt.

Three ideas make it honest:

1. **A turn is one cancellable task.** Planning, speaking, and running tools all
   execute inside a single `asyncio` Task. A barge-in cancels that Task, which
   unwinds every in-flight `await` — including the HTTP request to n8n.

2. **Cancelled ≠ didn't happen.** When a tool is cancelled, the executor knows
   whether the request had already left the machine:
   - cancelled **before** the request left → `CANCELLED` (nothing happened)
   - cancelled **while in flight** → `UNCERTAIN` (n8n may have executed it)

   It never pretends an uncertain effect is clean.

3. **A pending-effects ledger, persisted across chained interruptions.** Every
   effect that might still be live — completed creates, plus interrupted calls
   marked `UNCERTAIN` — goes into a ledger. The re-planner reconciles **all** of
   them, not just the last one. So if you say Monday → *Tuesday* → *Wednesday* in
   rapid barge-ins, it deletes the still-uncertain Monday event before booking
   Wednesday, instead of leaving a phantom on your calendar.

`send_email` is treated as terminal (you can't un-send), so the planner confirms
intent before firing rather than relying on a compensation.

---

## Architecture

```
 browser (voice + telemetry UI)
        │  WebSocket  (utterances, barge-in, cancel  ▲   state/plan/tool events ▼)
        ▼
 FastAPI  ──  Orchestrator (async state machine)
                 ├── Planner        provider-agnostic: mock | anthropic | openai | deepseek
                 └── ToolExecutor    → n8n webhooks (or mock mode)
                                      carries Idempotency-Key, tracks CANCELLED vs UNCERTAIN
```

**State machine:** `IDLE → PLANNING → SPEAKING → EXECUTING`, with `INTERRUPTED
→ REPLANNING` splicing in whenever the user barges. A single `asyncio.Lock`
guards the transitions so a barge-in and a finishing tool can't race.

### Backend (`backend/`)
| File               | Role                                                                 |
|--------------------|----------------------------------------------------------------------|
| `orchestrator.py`  | The core. Turn-as-task, barge-in handling, the ledger, reconciliation.|
| `planner.py`       | Turns utterances into JSON plans. Real LLMs + a rule-based mock.      |
| `tools.py`         | Runs tools against n8n; the CANCELLED-vs-UNCERTAIN honesty lives here.|
| `models.py`        | Pydantic models + the WebSocket wire protocol.                       |
| `main.py`          | FastAPI app: serves the UI, `/health`, and the `/ws` endpoint.        |

### Frontend (`frontend/`)
A dark "signal monitor" UI: a live oscilloscope whose amplitude and color track
the agent's state (teal = calm, coral = interruption), the current plan rendered
step-by-step on the right, and an event log that shows the plan being torn down
and rebuilt when you barge in. `index.html` / `styles.css` / `app.js`, no build
step.

---

## Running it

Requires Python 3.11+.

```bash
cd backend
pip install -r requirements.txt        # add --break-system-packages if your env needs it
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

Open **http://localhost:8000**. With no configuration this runs fully mocked —
rule-based planner, simulated tools — so you can try the barge-in loop
immediately, offline, with no API keys.

**Try it:** click **Schedule Monday**, wait until it's mid-response, then hit
**⚡ "change to Tuesday"** and watch the plan reconcile.

### Turning on the real thing
Copy `.env.example` to `.env` and fill in what you want:
- a real planner → set `LLM_PROVIDER` + `LLM_API_KEY` (see `.env.example`)
- real actions → set `N8N_BASE_URL` and build the six webhooks in
  [`n8n/example-workflows.md`](n8n/example-workflows.md)

Each layer is independent: real LLM + mock tools, or mock planner + real n8n,
both work.

---

## Voice UX notes (the honest limitations)

- **Hold-to-talk, by design.** In a browser tab the agent's speech and your mic
  share one acoustic space with no echo cancellation between them, so an
  always-listening mic would hear the agent and interrupt itself. Holding the
  mic to speak makes the barge-in intent explicit and dodges the echo problem.
  Pressing the mic **while the agent is talking is itself the barge-in.**
- **Instant audio stop.** On speech-start the local TTS is killed immediately —
  no server round-trip — so the interruption *feels* instant. The actual
  re-plan is sent once your final transcript is ready.
- **Firefox / no-SpeechRecognition browsers.** Web Speech STT isn't everywhere.
  The text console (and the quick-action buttons) is a first-class fallback and
  drives the exact same barge-in path; an utterance that arrives while the agent
  is busy is automatically treated as a barge-in.

---

## Testing

```bash
cd backend
# fast settings so tools/speech don't take real seconds
export MOCK_TOOL_LATENCY=1.0 SPEAK_SECONDS_PER_WORD=0.02 SPEAK_SECONDS_CAP=0.2

python test_orchestrator.py   # canonical: Monday → barge mid-call → Tuesday
python test_robustness.py     # "cancel that" undo; rapid double barge-in (→ Wednesday)
python test_ws.py             # end-to-end over a live WebSocket (start the server first)
```

`test_orchestrator.py` and `test_robustness.py` assert the ledger actually marks
the interrupted Monday create as `uncertain` and that the re-plan deletes it
before rebooking — i.e. that interruption recovery is real, not cosmetic.
