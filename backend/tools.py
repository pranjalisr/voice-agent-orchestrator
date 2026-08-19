"""
Tool executor — the bridge to the n8n workflows that act as the agent's hands.

Each tool is one n8n webhook. The executor's job is to make a call that can be
torn down cleanly the instant the user barges in, and to be honest about what
that teardown means:

  - Cancelled before the HTTP request left  -> status CANCELLED (nothing happened)
  - Cancelled while the request was in flight -> status UNCERTAIN (n8n may have
    run the workflow; the re-planner is told and can verify or compensate)

Every call carries an `Idempotency-Key` header so that a workflow which does
see a duplicate (e.g. a retry after an uncertain cancel) can de-dupe instead of
double-acting. Set `N8N_BASE_URL` to your instance; leave it unset to run in
mock mode with simulated latency, which is what makes the cancellation path
easy to see and test.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import httpx

from models import ToolCall, ToolStatus


class ToolExecutor:
    def __init__(self) -> None:
        self.base_url = os.getenv("N8N_BASE_URL", "").rstrip("/")
        self.mock = not self.base_url
        # Mapping tool name -> n8n webhook path. Override via env if your
        # webhooks live at different paths.
        self.webhook_prefix = os.getenv("N8N_WEBHOOK_PREFIX", "/webhook")
        # Simulated per-tool latency for mock mode (seconds).
        self._mock_latency = float(os.getenv("MOCK_TOOL_LATENCY", "2.5"))

    def _url(self, tool_name: str) -> str:
        return f"{self.base_url}{self.webhook_prefix}/{tool_name}"

    async def run(self, tool: ToolCall) -> ToolCall:
        """
        Execute a tool call. Designed to be run inside an asyncio Task that the
        orchestrator may cancel. On cancellation we set an accurate status and
        re-raise so the orchestrator's teardown sees the CancelledError.
        """
        tool.status = ToolStatus.RUNNING
        tool.started_at = time.time()

        try:
            if self.mock:
                await self._run_mock(tool)
            else:
                await self._run_real(tool)

            tool.status = ToolStatus.COMPLETED
            tool.ended_at = time.time()
            return tool

        except asyncio.CancelledError:
            # This is the barge-in teardown path. Whether the effect may have
            # landed depends on whether the request had already left when the
            # cancel arrived — true for both real and mock modes.
            tool.ended_at = time.time()
            if self.request_has_left(tool):
                # The workflow may already be running on n8n's side.
                tool.status = ToolStatus.UNCERTAIN
                tool.error = "cancelled while request in flight; effect uncertain"
            else:
                tool.status = ToolStatus.CANCELLED
                tool.error = "cancelled before execution"
            raise  # let the orchestrator observe the cancellation

        except Exception as exc:  # noqa: BLE001 — report any tool failure cleanly
            tool.status = ToolStatus.FAILED
            tool.error = str(exc)
            tool.ended_at = time.time()
            return tool

    # ------------------------------------------------------------------ #
    async def _run_real(self, tool: ToolCall) -> None:
        # A cancel during this await closes the connection; n8n may or may not
        # have started the workflow, which is exactly the UNCERTAIN case.
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                self._url(tool.name),
                headers={"Idempotency-Key": tool.idempotency_key},
                json=tool.args,
            )
            r.raise_for_status()
            try:
                tool.result = r.json()
            except Exception:  # noqa: BLE001 — non-JSON webhook responses
                tool.result = {"raw": r.text}

    async def _run_mock(self, tool: ToolCall) -> None:
        # Sleep in small slices so cancellation is responsive and so a cancel
        # that happens "mid-request" is realistic. The request is considered
        # to have left once we're past the first slice.
        slices = 10
        per = self._mock_latency / slices
        for _ in range(slices):
            await asyncio.sleep(per)
        tool.result = _mock_result(tool)

    # For mock mode we approximate "request left" using elapsed time so the
    # UNCERTAIN vs CANCELLED distinction still shows up in tests.
    def request_has_left(self, tool: ToolCall) -> bool:
        if not tool.started_at:
            return False
        # Consider the request "in flight" after the first latency slice.
        return (time.time() - tool.started_at) > (self._mock_latency / 10)


def _mock_result(tool: ToolCall) -> dict[str, Any]:
    name = tool.name
    a = tool.args
    if name == "create_calendar_event":
        return {"event_id": f"evt_{tool.idempotency_key[-6:]}",
                "title": a.get("title"), "day": a.get("day"), "time": a.get("time")}
    if name == "delete_calendar_event":
        return {"deleted": True, "title": a.get("title"), "day": a.get("day")}
    if name == "send_email":
        return {"message_id": f"msg_{tool.idempotency_key[-6:]}", "to": a.get("to")}
    if name == "add_task":
        return {"task_id": f"task_{tool.idempotency_key[-6:]}", "title": a.get("title")}
    if name == "remove_task":
        return {"removed": True, "title": a.get("title")}
    if name == "search_web":
        return {"results": [f"result about '{a.get('query')}'"]}
    return {"ok": True}
