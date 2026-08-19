"""
Data models for the voice-agent orchestrator.

Everything that crosses a boundary — the WebSocket wire, the planner's JSON
output, the orchestrator's internal state — is defined here so there is a
single source of truth for shape and vocabulary.
"""
from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# --------------------------------------------------------------------------- #
# Orchestrator state machine
# --------------------------------------------------------------------------- #
class AgentState(str, Enum):
    """The states the orchestrator can be in for a given conversation."""

    IDLE = "idle"            # waiting for the user to say something
    PLANNING = "planning"    # the planner is turning intent into a plan
    SPEAKING = "speaking"    # a spoken response is being delivered
    EXECUTING = "executing"  # a tool call (n8n workflow) is running
    INTERRUPTED = "interrupted"  # barge-in caught, tearing the current turn down
    REPLANNING = "replanning"    # folding the interruption into a new plan


# --------------------------------------------------------------------------- #
# Tool calls (the n8n "hands")
# --------------------------------------------------------------------------- #
class ToolStatus(str, Enum):
    PENDING = "pending"        # planned, not started
    RUNNING = "running"        # request in flight to n8n
    COMPLETED = "completed"    # finished, result captured
    FAILED = "failed"          # finished with an error
    CANCELLED = "cancelled"    # torn down cleanly before it started real work
    UNCERTAIN = "uncertain"    # cancelled mid-flight — side effect may have landed
    COMPENSATED = "compensated"  # a prior effect was undone by a compensating call


class ToolCall(BaseModel):
    """
    A single invocation of an n8n workflow.

    `idempotency_key` is sent to n8n so a retried or double-fired call is
    de-duplicated on the backend rather than executed twice. `reversible`
    plus `compensation` let the re-planner undo an effect that already
    landed (e.g. delete the Monday calendar event before booking Tuesday).
    """

    id: str = Field(default_factory=lambda: _id("tool"))
    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(default_factory=lambda: _id("idem"))

    reversible: bool = False
    # Name of the workflow that undoes this one, if any.
    compensation: Optional[str] = None

    status: ToolStatus = ToolStatus.PENDING
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None

    started_at: Optional[float] = None
    ended_at: Optional[float] = None

    def summary(self) -> dict[str, Any]:
        """A compact view handed to the planner when it re-plans."""
        return {
            "name": self.name,
            "args": self.args,
            "status": self.status.value,
            "result": self.result,
            "error": self.error,
            "reversible": self.reversible,
        }


# --------------------------------------------------------------------------- #
# Plans
# --------------------------------------------------------------------------- #
class PlanStep(BaseModel):
    """
    One step of a plan. A step either says something to the user, calls a
    tool, or both (say a line, then act). The planner emits these in order.
    """

    id: str = Field(default_factory=lambda: _id("step"))
    say: Optional[str] = None          # spoken before the tool runs
    tool: Optional[ToolCall] = None    # the workflow to invoke, if any


class Plan(BaseModel):
    id: str = Field(default_factory=lambda: _id("plan"))
    goal: str                          # the user's intent, in the planner's words
    steps: list[PlanStep] = Field(default_factory=list)
    # index of the step currently being worked; -1 before start
    cursor: int = -1

    def current(self) -> Optional[PlanStep]:
        if 0 <= self.cursor < len(self.steps):
            return self.steps[self.cursor]
        return None


# --------------------------------------------------------------------------- #
# Turn history — the memory the re-planner reasons over
# --------------------------------------------------------------------------- #
class TurnRecord(BaseModel):
    """What actually happened during one attempt at satisfying the user."""

    id: str = Field(default_factory=lambda: _id("turn"))
    user_utterance: str
    plan: Optional[Plan] = None
    completed_tools: list[ToolCall] = Field(default_factory=list)
    interrupted_tool: Optional[ToolCall] = None
    interruption_utterance: Optional[str] = None
    # Effects that may still be live on the backend and haven't been
    # definitively undone — carried across chained interruptions so the
    # re-planner can reconcile all of them, not just the last one.
    pending_effects: list[ToolCall] = Field(default_factory=list)
    created_at: float = Field(default_factory=time.time)


# --------------------------------------------------------------------------- #
# WebSocket wire protocol
# --------------------------------------------------------------------------- #
# Client -> server
class ClientEventType(str, Enum):
    USER_UTTERANCE = "user_utterance"    # a finalized transcript
    BARGE_IN = "barge_in"                # user started talking over the agent
    CANCEL = "cancel"                    # explicit stop button


# Server -> client
class ServerEventType(str, Enum):
    STATE = "state"              # state machine transitioned
    PLANNED = "planned"          # an initial plan is ready
    SAY = "say"                  # text the client should speak (TTS)
    TOOL_START = "tool_start"    # a workflow began
    TOOL_END = "tool_end"        # a workflow finished/failed/cancelled
    INTERRUPTED = "interrupted"  # confirmation the barge-in was handled
    REPLANNED = "replanned"      # a new plan is in effect
    DONE = "done"               # turn finished, back to idle
    ERROR = "error"


class ClientEvent(BaseModel):
    type: ClientEventType
    text: str = ""


class ServerEvent(BaseModel):
    type: ServerEventType
    # free-form payload; kept permissive so the frontend stays simple
    data: dict[str, Any] = Field(default_factory=dict)

    @staticmethod
    def state(state: AgentState) -> "ServerEvent":
        return ServerEvent(type=ServerEventType.STATE, data={"state": state.value})

    @staticmethod
    def say(text: str, interruptible: bool = True) -> "ServerEvent":
        return ServerEvent(
            type=ServerEventType.SAY,
            data={"text": text, "interruptible": interruptible},
        )
