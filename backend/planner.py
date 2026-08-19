"""
Planning and re-planning.

The planner turns a user utterance (plus, on a re-plan, the full history of
what already happened) into a `Plan`. It is provider-agnostic: point it at
Anthropic, OpenAI, or DeepSeek with an env var, or run the built-in mock
planner with no key at all so the orchestration loop is fully testable
offline.

The re-plan path is the interesting one. It is handed:
  - the original goal,
  - every tool that already completed (with results),
  - the tool that was in flight when the user barged in (status UNCERTAIN),
  - the interruption utterance itself,
and must return a plan that reconciles all of it — including undoing an
effect that already landed when the user changed their mind.
"""
from __future__ import annotations

import json
import os
import re
from typing import Optional

import httpx

from models import Plan, PlanStep, ToolCall, TurnRecord

# --------------------------------------------------------------------------- #
# Tool catalogue — mirrors the n8n workflows exposed as webhooks.
# Keep this in sync with n8n/example-workflows.md.
# --------------------------------------------------------------------------- #
TOOL_CATALOGUE = {
    "create_calendar_event": {
        "description": "Create a calendar event.",
        "args": ["title", "day", "time"],
        "reversible": True,
        "compensation": "delete_calendar_event",
    },
    "delete_calendar_event": {
        "description": "Delete a previously created calendar event.",
        "args": ["title", "day"],
        "reversible": False,
        "compensation": None,
    },
    "send_email": {
        "description": "Send an email.",
        "args": ["to", "subject", "body"],
        "reversible": False,  # can't un-send; planner must confirm before firing
        "compensation": None,
    },
    "add_task": {
        "description": "Add a task to the task list.",
        "args": ["title", "due"],
        "reversible": True,
        "compensation": "remove_task",
    },
    "remove_task": {
        "description": "Remove a task from the task list.",
        "args": ["title"],
        "reversible": False,
        "compensation": None,
    },
    "search_web": {
        "description": "Search the web for information (read-only).",
        "args": ["query"],
        "reversible": False,  # read-only, nothing to undo
        "compensation": None,
    },
}


SYSTEM_PROMPT = """You are the planner for a voice assistant. You turn what the \
user said into a short ordered plan of steps. Each step may speak a line to the \
user and/or call one tool.

You MUST reply with only a JSON object, no prose, no markdown fences, shaped like:
{
  "goal": "<one-line restatement of what the user wants>",
  "steps": [
    {"say": "<optional line to speak>", "tool": {"name": "<tool>", "args": {...}}},
    {"say": "<optional line, no tool this step>"}
  ]
}

Rules:
- Keep spoken lines short and natural — this is voice, not text.
- Only use tools from the provided catalogue.
- For irreversible tools (send_email), speak a one-line confirmation step first.
- When RE-PLANNING after an interruption, you are told what already ran. If an
  effect that already landed now contradicts the user's new instruction, undo it
  with its compensation tool BEFORE doing the new thing. Do not repeat work that
  already succeeded and is still valid.
- Acknowledge the interruption in your first spoken line when re-planning."""


class Planner:
    def __init__(self) -> None:
        self.provider = os.getenv("LLM_PROVIDER", "mock").lower()
        self.api_key = os.getenv("LLM_API_KEY", "")
        self.model = os.getenv("LLM_MODEL", "")
        if self.provider != "mock" and not self.api_key:
            # No key despite a provider being named — fall back so nothing breaks.
            self.provider = "mock"

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    async def plan(self, utterance: str) -> Plan:
        prompt = self._initial_prompt(utterance)
        raw = await self._complete(prompt)
        return self._parse(raw, fallback_goal=utterance)

    async def replan(self, history: TurnRecord) -> Plan:
        prompt = self._replan_prompt(history)
        raw = await self._complete(prompt)
        goal = history.plan.goal if history.plan else history.user_utterance
        return self._parse(raw, fallback_goal=goal)

    # ------------------------------------------------------------------ #
    # Prompt construction
    # ------------------------------------------------------------------ #
    def _catalogue_text(self) -> str:
        lines = []
        for name, spec in TOOL_CATALOGUE.items():
            lines.append(
                f"- {name}({', '.join(spec['args'])}): {spec['description']}"
                + ("" if spec["reversible"] else " [irreversible]")
            )
        return "\n".join(lines)

    def _initial_prompt(self, utterance: str) -> str:
        return (
            f"Tool catalogue:\n{self._catalogue_text()}\n\n"
            f'The user said: "{utterance}"\n\n'
            "Produce the plan JSON."
        )

    def _replan_prompt(self, h: TurnRecord) -> str:
        completed = [t.summary() for t in h.completed_tools]
        interrupted = h.interrupted_tool.summary() if h.interrupted_tool else None
        pending = [t.summary() for t in h.pending_effects]
        return (
            f"Tool catalogue:\n{self._catalogue_text()}\n\n"
            f'Original goal: "{h.plan.goal if h.plan else h.user_utterance}"\n'
            f"Tools that already completed: {json.dumps(completed)}\n"
            f"Tool that was in flight when the user interrupted "
            f"(may or may not have taken effect): {json.dumps(interrupted)}\n"
            f"Effects that may still be live and are not yet undone "
            f"(compensate any that now contradict the user): {json.dumps(pending)}\n"
            f'The user interrupted and said: "{h.interruption_utterance}"\n\n'
            "Produce a NEW plan JSON that honours the interruption. First undo "
            "every still-live effect that contradicts the new instruction using "
            "its compensation tool, then carry out what the user now wants."
        )

    # ------------------------------------------------------------------ #
    # Provider dispatch
    # ------------------------------------------------------------------ #
    async def _complete(self, prompt: str) -> str:
        if self.provider == "anthropic":
            return await self._anthropic(prompt)
        if self.provider in ("openai", "deepseek"):
            return await self._openai_compatible(prompt)
        return self._mock(prompt)

    async def _anthropic(self, prompt: str) -> str:
        model = self.model or "claude-sonnet-4-6"
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": 1024,
                    "system": SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            r.raise_for_status()
            data = r.json()
            return "".join(
                b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
            )

    async def _openai_compatible(self, prompt: str) -> str:
        base = (
            "https://api.deepseek.com/v1"
            if self.provider == "deepseek"
            else "https://api.openai.com/v1"
        )
        model = self.model or ("deepseek-chat" if self.provider == "deepseek" else "gpt-4o")
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    "response_format": {"type": "json_object"},
                },
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]

    # ------------------------------------------------------------------ #
    # Mock planner — rule-based, good enough to demonstrate the loop offline.
    # It understands the canonical "schedule then change the day" scenario
    # plus a few others, and crucially emits a *compensating* step on re-plan.
    # ------------------------------------------------------------------ #
    def _mock(self, prompt: str) -> str:
        is_replan = "The user interrupted and said" in prompt
        return self._mock_replan(prompt) if is_replan else self._mock_initial(prompt)

    def _mock_initial(self, prompt: str) -> str:
        utt = self._extract(prompt, r'The user said: "(.*?)"')
        low = utt.lower()

        # This is a *fresh* turn (no prior plan to reconcile against — either
        # the user's first utterance, or a barge-in that arrived after the
        # previous turn had already finished). "Cancel" and "change/move it"
        # only make sense relative to something in flight, so handle them
        # here too instead of falling through to the generic web-search
        # catch-all below, which otherwise reads as nonsense in the demo
        # (e.g. clicking the "cancel that" quick-button before anything was
        # ever scheduled).
        if any(w in low for w in
               ("cancel", "never mind", "nevermind", "forget", "scrap")):
            return json.dumps({
                "goal": "Nothing to cancel",
                "steps": [{"say": "There's nothing in progress to cancel."}],
            })

        day = self._find_day(low) or "Monday"
        time_ = self._find_time(low) or "10am"

        if any(w in low for w in ("schedule", "meeting", "calendar", "book", "set up",
                                   "change", "reschedul", "move")):
            title = self._find_title(utt) or "Meeting"
            return json.dumps({
                "goal": f"Schedule '{title}' for {day} at {time_}",
                "steps": [
                    {"say": f"Sure, scheduling {title} for {day} at {time_}."},
                    {"tool": {"name": "create_calendar_event",
                              "args": {"title": title, "day": day, "time": time_}}},
                    {"say": "Done — it's on your calendar."},
                ],
            })
        if "email" in low or "send" in low:
            return json.dumps({
                "goal": "Send an email",
                "steps": [
                    {"say": "I'll send that email — confirming before I do."},
                    {"tool": {"name": "send_email",
                              "args": {"to": "team@example.com",
                                       "subject": "Update", "body": utt}}},
                    {"say": "Sent."},
                ],
            })
        if "task" in low or "remind" in low or "todo" in low:
            title = self._find_title(utt) or "New task"
            return json.dumps({
                "goal": f"Add task '{title}'",
                "steps": [
                    {"tool": {"name": "add_task", "args": {"title": title, "due": day}}},
                    {"say": f"Added {title} to your list for {day}."},
                ],
            })
        return json.dumps({
            "goal": utt or "Look something up",
            "steps": [
                {"say": "Let me look that up."},
                {"tool": {"name": "search_web", "args": {"query": utt}}},
                {"say": "Here's what I found."},
            ],
        })

    def _mock_replan(self, prompt: str) -> str:
        interruption = self._extract(prompt, r'The user interrupted and said: "(.*?)"')
        low = interruption.lower()
        goal = self._extract(prompt, r'Original goal: "(.*?)"')

        interrupted = self._json_obj(
            self._extract(prompt, r"in flight when the user interrupted.*?: (\{.*?\}|null)\n"))
        pending = self._json_arr(
            self._extract(prompt, r"may still be live.*?: (\[.*?\])\nThe user"))

        # Every calendar event that might still be live, newest first.
        live_events = [e for e in pending if e.get("name") == "create_calendar_event"]
        live_tasks = [e for e in pending if e.get("name") == "add_task"]

        # Recover the subject title from live effects, the interrupted tool,
        # or the goal string — in that order of reliability.
        title = None
        if live_events:
            title = live_events[-1].get("args", {}).get("title")
        if not title and interrupted and interrupted.get("name") == "create_calendar_event":
            title = interrupted.get("args", {}).get("title")
        if not title:
            m = re.search(r"'([^']+)'", goal)
            title = m.group(1) if m else "Meeting"

        new_day = self._find_day(low)
        new_time = self._find_time(low)
        is_cancel = any(w in low for w in
                        ("cancel", "never mind", "nevermind", "forget", "scrap"))
        rescheduling = bool(new_day or new_time) and (
            live_events or (interrupted and interrupted.get("name") == "create_calendar_event")
            or "schedul" in goal.lower() or "reschedul" in goal.lower())

        # ---- Reschedule: undo any live event, then book the new day. -------- #
        if rescheduling and not is_cancel:
            last_time = None
            if live_events:
                last_time = live_events[-1].get("args", {}).get("time")
            day = new_day or (live_events[-1]["args"].get("day") if live_events else "Monday")
            time_ = new_time or last_time or "10am"

            steps = [{"say": f"Got it — moving it to {day}"
                             + (f" at {time_}" if new_time else "") + "."}]
            # Compensate every possibly-live event (covers chained changes).
            for ev in live_events:
                steps.append({"tool": {"name": "delete_calendar_event",
                                       "args": {"title": ev["args"].get("title", title),
                                                "day": ev["args"].get("day")}}})
            steps.append({"tool": {"name": "create_calendar_event",
                                   "args": {"title": title, "day": day, "time": time_}}})
            steps.append({"say": f"Done — {title} is now on {day}."})
            return json.dumps({"goal": f"Reschedule '{title}' to {day}", "steps": steps})

        # ---- Cancel: undo everything still live. --------------------------- #
        if is_cancel:
            steps = [{"say": "Okay, cancelling that."}]
            for ev in live_events:
                steps.append({"tool": {"name": "delete_calendar_event",
                                       "args": {"title": ev["args"].get("title", title),
                                                "day": ev["args"].get("day")}}})
            for tk in live_tasks:
                steps.append({"tool": {"name": "remove_task",
                                       "args": {"title": tk["args"].get("title")}}})
            steps.append({"say": "All cleared — nothing left on your calendar for that."
                          if (live_events or live_tasks)
                          else "Nothing was finalized, so you're all set."})
            return json.dumps({"goal": "Cancel the in-flight action", "steps": steps})

        # ---- Otherwise: a genuinely new request. First tidy live effects. -- #
        fresh = json.loads(self._mock_initial(f'The user said: "{interruption}"'))
        cleanup = []
        for ev in live_events:
            cleanup.append({"tool": {"name": "delete_calendar_event",
                                     "args": {"title": ev["args"].get("title", title),
                                              "day": ev["args"].get("day")}}})
        if fresh["steps"] and fresh["steps"][0].get("say"):
            fresh["steps"][0]["say"] = "Sure — " + fresh["steps"][0]["say"]
        fresh["steps"] = cleanup + fresh["steps"]
        return json.dumps(fresh)

    # ------------------------------------------------------------------ #
    # Small extraction helpers for the mock planner
    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract(text: str, pattern: str) -> str:
        m = re.search(pattern, text, re.DOTALL)
        return m.group(1).strip() if m else ""

    @staticmethod
    def _json_obj(s: str) -> dict:
        try:
            v = json.loads(s) if s else {}
            return v if isinstance(v, dict) else {}
        except json.JSONDecodeError:
            return {}

    @staticmethod
    def _json_arr(s: str) -> list:
        try:
            v = json.loads(s) if s else []
            return v if isinstance(v, list) else []
        except json.JSONDecodeError:
            return []

    @staticmethod
    def _find_day(text: str) -> Optional[str]:
        days = ["monday", "tuesday", "wednesday", "thursday",
                "friday", "saturday", "sunday", "today", "tomorrow"]
        for d in days:
            if re.search(rf"\b{d}\b", text):
                return d.capitalize()
        return None

    @staticmethod
    def _find_time(text: str) -> Optional[str]:
        m = re.search(r"\b(\d{1,2}(?::\d{2})?\s*(?:am|pm))\b", text)
        if m:
            return m.group(1).replace(" ", "")
        return None

    @staticmethod
    def _find_title(text: str) -> Optional[str]:
        m = re.search(r"(?:called|titled|named|about|for)\s+([A-Za-z0-9 ]{2,30})", text)
        if m:
            title = m.group(1).strip()
            # Trim trailing day/time words the regex may have swept up.
            title = re.split(r"\b(on|at|for|tomorrow|today|monday|tuesday|wednesday|"
                             r"thursday|friday|saturday|sunday)\b", title, flags=re.I)[0]
            return title.strip().title() or None
        return None

    # ------------------------------------------------------------------ #
    # Parsing the model's JSON into a Plan
    # ------------------------------------------------------------------ #
    def _parse(self, raw: str, fallback_goal: str) -> Plan:
        text = raw.strip()
        # Strip markdown fences if a model added them despite instructions.
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
            text = re.sub(r"\n?```$", "", text).strip()
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            # Last-ditch: find the first {...} block.
            m = re.search(r"\{.*\}", text, re.DOTALL)
            obj = json.loads(m.group(0)) if m else {"goal": fallback_goal, "steps": []}

        steps: list[PlanStep] = []
        for s in obj.get("steps", []):
            tool = None
            if s.get("tool"):
                t = s["tool"]
                spec = TOOL_CATALOGUE.get(t.get("name", ""), {})
                tool = ToolCall(
                    name=t.get("name", "unknown"),
                    args=t.get("args", {}),
                    reversible=spec.get("reversible", False),
                    compensation=spec.get("compensation"),
                )
            steps.append(PlanStep(say=s.get("say"), tool=tool))

        return Plan(goal=obj.get("goal", fallback_goal), steps=steps)
