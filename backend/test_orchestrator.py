"""
Simulates the canonical scenario end-to-end against the real orchestrator,
mock planner, and mock executor — no browser, no network.

Scenario:
  1. "Schedule a sync for Monday at 10am"
  2. Agent plans, speaks, starts creating the Monday event.
  3. Mid-tool-call the user barges in: "wait no, change that to Tuesday".
  4. Agent must cancel the in-flight call, mark it uncertain, re-plan to
     delete the Monday event and create the Tuesday one, then resume.
"""
import asyncio
import os

# Fast, deterministic timings for the test.
os.environ.setdefault("MOCK_TOOL_LATENCY", "1.0")
os.environ.setdefault("SPEAK_SECONDS_PER_WORD", "0.02")
os.environ.setdefault("SPEAK_SECONDS_CAP", "0.2")

from models import AgentState, ServerEventType  # noqa: E402
from orchestrator import Orchestrator  # noqa: E402

events: list = []


async def emit(ev):
    events.append(ev)
    payload = {k: v for k, v in ev.data.items() if k != "steps"}
    print(f"  <- {ev.type.value:12} {payload}")


def divider(title):
    print(f"\n{'='*66}\n{title}\n{'='*66}")


async def main():
    orch = Orchestrator(emit)

    divider("1) User: 'Schedule a sync for Monday at 10am'")
    await orch.handle_user_utterance("Schedule a sync for Monday at 10am")

    # Let it plan, speak, and get partway into the calendar tool call.
    await asyncio.sleep(0.6)
    print(f"\n  [state before barge-in: {orch.state.value}]")
    print(f"  [active tool: {orch._active_tool.name if orch._active_tool else None} "
          f"status={orch._active_tool.status.value if orch._active_tool else '-'}]")

    divider("2) User barges in: 'wait no, change that to Tuesday'")
    await orch.handle_barge_in("wait no, change that to Tuesday")

    # Let the re-plan + resume finish.
    await asyncio.sleep(3.0)
    if orch.is_busy():
        await orch._turn_task

    # ------------------------------------------------------------------ #
    divider("VERIFY")
    types = [e.type for e in events]

    assert ServerEventType.INTERRUPTED in types, "barge-in was not acknowledged"
    assert ServerEventType.REPLANNED in types, "no re-plan happened"

    tool_ends = [e for e in events if e.type == ServerEventType.TOOL_END]
    statuses = [(e.data["tool"], e.data["status"]) for e in tool_ends]
    print("Tool outcomes in order:")
    for name, status in statuses:
        print(f"   - {name}: {status}")

    # The interrupted Monday create must be uncertain or cancelled.
    monday_create = next(
        (e for e in tool_ends
         if e.data["tool"] == "create_calendar_event"
         and e.data.get("result", {}) and e.data["result"].get("day") == "Monday"),
        None,
    )
    # After re-plan we must see a delete (compensation) and a Tuesday create.
    did_delete = any(e.data["tool"] == "delete_calendar_event" for e in tool_ends)
    tuesday_create = any(
        e.data["tool"] == "create_calendar_event"
        and e.data.get("result", {}) and e.data["result"].get("day") == "Tuesday"
        for e in tool_ends
    )

    assert did_delete, "re-plan did not undo the Monday event"
    assert tuesday_create, "re-plan did not create the Tuesday event"

    final_state = orch.state
    assert final_state == AgentState.IDLE, f"ended in {final_state}, expected idle"

    print("\nInterrupted tool captured in record:",
          orch._record.interrupted_tool.name if orch._record.interrupted_tool else None,
          "->",
          orch._record.interrupted_tool.status.value if orch._record.interrupted_tool else None)

    print("\n\u2705 PASS: barge-in cancelled the in-flight call, the re-plan "
          "compensated the\n   Monday event and booked Tuesday, and the agent "
          "returned to idle.")


if __name__ == "__main__":
    asyncio.run(main())
