"""Edge cases: outright cancel, and two barge-ins in quick succession."""
import asyncio
import os

os.environ.setdefault("MOCK_TOOL_LATENCY", "1.0")
os.environ.setdefault("SPEAK_SECONDS_PER_WORD", "0.02")
os.environ.setdefault("SPEAK_SECONDS_CAP", "0.2")

from models import AgentState, ServerEventType  # noqa: E402
from orchestrator import Orchestrator  # noqa: E402


async def run_case(name, script):
    print(f"\n{'='*66}\n{name}\n{'='*66}")
    events = []

    async def emit(ev):
        events.append(ev)
        if ev.type in (ServerEventType.TOOL_START, ServerEventType.TOOL_END,
                       ServerEventType.INTERRUPTED, ServerEventType.REPLANNED,
                       ServerEventType.DONE):
            print(f"  <- {ev.type.value:11} "
                  f"{ {k:v for k,v in ev.data.items() if k not in ('steps','result')} }")

    orch = Orchestrator(emit)
    await script(orch)
    if orch.is_busy():
        try:
            await orch._turn_task
        except asyncio.CancelledError:
            pass
    return orch, events


async def case_cancel(orch):
    await orch.handle_user_utterance("Schedule a sync for Monday at 10am")
    await asyncio.sleep(0.6)
    await orch.handle_cancel()
    await asyncio.sleep(2.0)


async def case_double(orch):
    await orch.handle_user_utterance("Schedule a sync for Monday at 10am")
    await asyncio.sleep(0.5)
    await orch.handle_barge_in("no wait, make it Tuesday")
    # Interrupt again almost immediately, before the first re-plan resumes.
    await asyncio.sleep(0.15)
    await orch.handle_barge_in("actually Wednesday")
    await asyncio.sleep(3.0)


async def main():
    orch, events = await run_case("A) 'cancel that' mid-call", case_cancel)
    assert orch.state == AgentState.IDLE
    assert any(e.type == ServerEventType.INTERRUPTED for e in events)
    # Compensation should have removed the (uncertain) Monday event.
    assert any(e.type == ServerEventType.TOOL_END
               and e.data["tool"] == "delete_calendar_event" for e in events), \
        "cancel did not undo the in-flight event"
    print("  \u2705 cancel undid the in-flight action and returned to idle")

    orch, events = await run_case("B) double barge-in (Tuesday then Wednesday)",
                                  case_double)
    assert orch.state == AgentState.IDLE, f"ended in {orch.state}"
    # Final create should target Wednesday, not Tuesday.
    creates = [e for e in events
               if e.type == ServerEventType.TOOL_END
               and e.data["tool"] == "create_calendar_event"
               and e.data.get("result")]
    final_day = creates[-1].data["result"]["day"] if creates else None
    print(f"  final booked day: {final_day}")
    assert final_day == "Wednesday", f"expected Wednesday, got {final_day}"
    print("  \u2705 rapid double interruption resolved to the latest instruction")

    print("\n\u2705 ALL ROBUSTNESS CASES PASS")


if __name__ == "__main__":
    asyncio.run(main())
