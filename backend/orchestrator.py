"""
The orchestrator — the part that makes interruption feel human.

A "turn" (planning + speaking + running tools) executes inside a single
cancellable asyncio Task. A barge-in cancels that Task, which unwinds every
in-flight await — including the HTTP call to n8n — and lets the tool executor
record whether an effect may have landed. The orchestrator then folds the
interruption into a fresh plan and resumes, reusing work that's still valid
and undoing work that the user just contradicted.

The design keeps three invariants:
  1. Exactly one turn Task runs at a time; barge-in always cancels it first.
  2. The in-flight tool is captured before the plan is thrown away, so the
     re-planner knows what to reconcile.
  3. State transitions are the only thing the frontend needs to follow, so
     every transition is emitted.
"""
from __future__ import annotations

import asyncio
import os
from typing import Awaitable, Callable, Optional

from models import (
    AgentState,
    Plan,
    ServerEvent,
    ServerEventType,
    ToolCall,
    ToolStatus,
    TurnRecord,
)
from planner import TOOL_CATALOGUE, Planner
from tools import ToolExecutor

# Names that act as compensations (they undo some other tool's effect).
_COMPENSATION_NAMES = {
    spec["compensation"] for spec in TOOL_CATALOGUE.values() if spec.get("compensation")
}

Emit = Callable[[ServerEvent], Awaitable[None]]


class Orchestrator:
    def __init__(self, emit: Emit,
                 planner: Optional[Planner] = None,
                 executor: Optional[ToolExecutor] = None) -> None:
        self.emit = emit
        self.planner = planner or Planner()
        self.executor = executor or ToolExecutor()

        self.state = AgentState.IDLE
        self.plan: Optional[Plan] = None

        self._record: Optional[TurnRecord] = None
        self._active_tool: Optional[ToolCall] = None
        self._turn_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

        # Effects that may be live on the backend right now (completed creates,
        # plus in-flight calls interrupted with an uncertain outcome). Persists
        # across re-plans within one conversational goal so nothing is orphaned.
        self._pending_effects: list[ToolCall] = []

        # Estimated speaking time so a barge-in *during speech* is possible
        # on the backend too, not just in the client's audio. Tunable for tests.
        self._speak_per_word = float(os.getenv("SPEAK_SECONDS_PER_WORD", "0.30"))
        self._speak_cap = float(os.getenv("SPEAK_SECONDS_CAP", "6.0"))

    # ------------------------------------------------------------------ #
    # State helpers
    # ------------------------------------------------------------------ #
    async def _set_state(self, state: AgentState) -> None:
        self.state = state
        await self.emit(ServerEvent.state(state))

    def is_busy(self) -> bool:
        return self._turn_task is not None and not self._turn_task.done()

    # ------------------------------------------------------------------ #
    # Entry points (called from the WebSocket loop)
    # ------------------------------------------------------------------ #
    async def handle_user_utterance(self, text: str) -> None:
        """A finalized transcript from the user."""
        async with self._lock:
            if self.is_busy():
                # Talking over an active turn is a barge-in, not a new turn.
                await self._interrupt_locked(text)
                return
            self._pending_effects = []  # new goal, clean slate
            self._record = TurnRecord(user_utterance=text)
            self._turn_task = asyncio.create_task(self._run_turn(replan=False))

    async def handle_barge_in(self, text: str) -> None:
        """User started speaking over the agent."""
        async with self._lock:
            if not self.is_busy():
                # Nothing to interrupt — treat as a normal utterance.
                if text:
                    self._record = TurnRecord(user_utterance=text)
                    self._turn_task = asyncio.create_task(self._run_turn(replan=False))
                return
            await self._interrupt_locked(text)

    async def handle_cancel(self) -> None:
        """Explicit stop. Routed through the same undo logic as a barge-in."""
        await self.handle_barge_in("cancel that")

    # ------------------------------------------------------------------ #
    # Interruption — the crux
    # ------------------------------------------------------------------ #
    async def _interrupt_locked(self, text: str) -> None:
        """Tear down the running turn and relaunch as a re-plan. Lock held."""
        await self._set_state(AgentState.INTERRUPTED)
        await self.emit(ServerEvent(
            type=ServerEventType.INTERRUPTED,
            data={"heard": text},
        ))

        # 1. Cancel the running turn and wait for it to unwind fully.
        task = self._turn_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 — a turn that errored while unwinding
                pass

        # 2. Capture what was in flight BEFORE the plan is discarded.
        assert self._record is not None
        self._record.interruption_utterance = text
        if self._active_tool and self._active_tool.status in (
            ToolStatus.UNCERTAIN, ToolStatus.CANCELLED, ToolStatus.RUNNING
        ):
            # RUNNING can linger if cancellation raced; normalize it.
            if self._active_tool.status is ToolStatus.RUNNING:
                self._active_tool.status = ToolStatus.UNCERTAIN
            self._record.interrupted_tool = self._active_tool
            # An uncertain effect may have landed — treat it as live.
            if self._active_tool.status is ToolStatus.UNCERTAIN:
                self._ledger_add_if_effect(self._active_tool)
            # Tell the frontend the in-flight call stopped, and how.
            await self.emit(ServerEvent(
                type=ServerEventType.TOOL_END,
                data={"tool": self._active_tool.name,
                      "status": self._active_tool.status.value,
                      "result": None, "id": self._active_tool.id},
            ))
        self._active_tool = None

        # 3. Relaunch as a re-plan turn.
        self._turn_task = asyncio.create_task(self._run_turn(replan=True))

    # ------------------------------------------------------------------ #
    # Turn execution
    # ------------------------------------------------------------------ #
    async def _run_turn(self, *, replan: bool) -> None:
        try:
            if replan:
                await self._set_state(AgentState.REPLANNING)
                # Hand the re-planner every effect that might still be live.
                self._record.pending_effects = list(self._pending_effects)  # type: ignore[union-attr]
                plan = await self.planner.replan(self._record)  # type: ignore[arg-type]
                await self.emit(ServerEvent(
                    type=ServerEventType.REPLANNED,
                    data={"goal": plan.goal,
                          "steps": [self._step_view(s) for s in plan.steps]},
                ))
            else:
                await self._set_state(AgentState.PLANNING)
                plan = await self.planner.plan(self._record.user_utterance)  # type: ignore[union-attr]
                await self.emit(ServerEvent(
                    type=ServerEventType.PLANNED,
                    data={"goal": plan.goal,
                          "steps": [self._step_view(s) for s in plan.steps]},
                ))

            self.plan = plan
            self._record.plan = plan  # type: ignore[union-attr]

            await self._execute_plan(plan)

            await self._set_state(AgentState.IDLE)
            await self.emit(ServerEvent(type=ServerEventType.DONE,
                                        data={"goal": plan.goal}))
        except asyncio.CancelledError:
            # Interrupted mid-turn; the interrupter handles the rest.
            raise
        except Exception as exc:  # noqa: BLE001
            await self.emit(ServerEvent(type=ServerEventType.ERROR,
                                        data={"message": str(exc)}))
            await self._set_state(AgentState.IDLE)

    async def _execute_plan(self, plan: Plan) -> None:
        for i, step in enumerate(plan.steps):
            plan.cursor = i

            if step.say:
                await self._set_state(AgentState.SPEAKING)
                await self.emit(ServerEvent.say(step.say))
                # Stay blocked (and interruptible) for the estimated speech
                # duration so a barge-in during speech cancels this task.
                await asyncio.sleep(self._speak_duration(step.say))

            if step.tool:
                await self._set_state(AgentState.EXECUTING)
                await self.emit(ServerEvent(
                    type=ServerEventType.TOOL_START,
                    data={"tool": step.tool.name, "args": step.tool.args,
                          "id": step.tool.id},
                ))
                self._active_tool = step.tool
                await self.executor.run(step.tool)  # cancellable
                self._active_tool = None
                assert self._record is not None
                self._record.completed_tools.append(step.tool)
                self._update_ledger(step.tool)
                await self.emit(ServerEvent(
                    type=ServerEventType.TOOL_END,
                    data={"tool": step.tool.name, "status": step.tool.status.value,
                          "result": step.tool.result, "id": step.tool.id},
                ))

    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    # Pending-effects ledger
    # ------------------------------------------------------------------ #
    def _ledger_add_if_effect(self, tool: ToolCall) -> None:
        """Record a tool as a live effect if it creates something undoable."""
        if tool.compensation:  # only creating tools carry a compensation
            # Avoid duplicates by (name, title).
            title = tool.args.get("title")
            for existing in self._pending_effects:
                if existing.name == tool.name and existing.args.get("title") == title:
                    return
            self._pending_effects.append(tool)

    def _update_ledger(self, tool: ToolCall) -> None:
        """After a tool COMPLETES, add creates and clear matching compensations."""
        if tool.status is not ToolStatus.COMPLETED:
            return
        if tool.name in _COMPENSATION_NAMES:
            # This undoes a prior create; drop the matching live effect(s).
            title = tool.args.get("title")
            self._pending_effects = [
                e for e in self._pending_effects
                if not (e.compensation == tool.name
                        and e.args.get("title") == title)
            ]
        else:
            self._ledger_add_if_effect(tool)

    def _speak_duration(self, text: str) -> float:
        words = max(1, len(text.split()))
        return min(self._speak_cap, words * self._speak_per_word)

    @staticmethod
    def _step_view(step) -> dict:
        return {
            "say": step.say,
            "tool": step.tool.name if step.tool else None,
            "args": step.tool.args if step.tool else None,
        }
