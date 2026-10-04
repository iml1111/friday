"""verify_deferred.py — Real-API check for deferred tools (Suspended / resume).

Confirms what fake-provider tests cannot: that the backend accepts the
histories the deferred flow produces.

  A. approve: the model calls the deferred `approval` tool (next to ExampleTool)
     → Suspended → the state crosses a JSON boundary → resume() with the
     approval → step() completes.
  B. cancel: same start → resume() with an is_error result → a new user message
     → step() completes.

Usage:
    LLM_MODEL=<model-id> python scripts/verify/verify_deferred.py

Cost guardrail: max_tokens=512; caller-side turn cap=4 per phase.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # scripts/ -> import _env
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root -> friday_agent (when not pip-installed)
from _env import create_config, create_provider, resolve_api_key
from friday_agent.core.engine import FridayAgent
from friday_agent.core.loop import pending_tool_uses, resume
from friday_agent.core.state import LoopState, Suspended, Terminal
from friday_agent.messages.types import create_user_message
from friday_agent.tools.base import Tool, ToolResult
from friday_agent.tools.builtin.example_tool import ExampleTool

_CAP = 4

SYSTEM_PROMPT = (
    "You publish documents for the user. Before publishing, ALWAYS call the approval "
    "tool with action='publish'. In the same response, also call ExampleTool once with "
    "payload='draft'. After the results arrive, reply in one short sentence."
)


class ApprovalInput(BaseModel):
    action: str = Field(description="The action that needs a human's approval")


class Approval(Tool):
    """Requests a human's approval for an action. The decision arrives later."""

    name = "approval"

    def input_schema(self) -> type[BaseModel]:
        return ApprovalInput

    def is_deferred(self, input: dict) -> bool:
        return True

    async def call(self, args: dict) -> ToolResult:
        return ToolResult(data="approval runs outside step()", is_error=True)


async def _run(engine: FridayAgent, state: LoopState) -> LoopState | Suspended | Terminal:
    """Step until the loop stops: a Suspended, a Terminal, or the turn cap."""
    outcome: LoopState | Suspended | Terminal = state
    for _ in range(_CAP):
        async for item in engine.step(outcome):
            if isinstance(item, (LoopState, Suspended, Terminal)):
                outcome = item
        if not isinstance(outcome, LoopState):
            break
    return outcome


def _describe(outcome) -> str:
    if isinstance(outcome, Suspended):
        return f"Suspended(pending={[b.name for b in outcome.pending]})"
    if isinstance(outcome, Terminal):
        return f"Terminal(reason={outcome.reason}, error={outcome.error!r})"
    return type(outcome).__name__


async def _phase(engine: FridayAgent, *, cancel: bool) -> dict[str, bool]:
    first = await _run(engine, LoopState(messages=[create_user_message("Publish the quarterly report.")]))
    print(f"  first : {_describe(first)}")
    if not isinstance(first, Suspended):
        return {"turn suspended on the deferred approval": False}

    state = LoopState.from_dict(json.loads(json.dumps(first.state.to_dict())))  # a process boundary
    pending = pending_tool_uses(state)
    if cancel:
        state = resume(state, {b.id: ToolResult(data="Cancelled by the user.", is_error=True) for b in pending})
        state.messages.append(create_user_message("Never mind, don't publish. Just reply OK."))
    else:
        state = resume(state, {b.id: ToolResult(data="Approved by the editor.") for b in pending})

    final = await _run(engine, state)
    print(f"  final : {_describe(final)}")
    return {
        "turn suspended on the deferred approval": True,
        "pending recomputed after the JSON round trip": [b.id for b in pending] == [b.id for b in first.pending],
        "API accepted the resumed history (reason == 'completed')":
            isinstance(final, Terminal) and final.reason == "completed",
    }


async def main() -> int:
    print("=" * 60)
    print("Verification: Deferred tools — Suspended / resume (Real API)")
    print("=" * 60)

    model = os.environ.get("LLM_MODEL", "")
    if not model:
        sys.exit("Set the LLM_MODEL environment variable to a real model ID.")

    engine = FridayAgent(
        provider=create_provider(model, api_key=resolve_api_key(model)),
        tools=[Approval(), ExampleTool()],
        system_prompt=SYSTEM_PROMPT,
        config=create_config(model, max_tokens=512),
    )

    checks: dict[str, bool] = {}
    for name, cancel in (("A. approve", False), ("B. cancel", True)):
        print(f"\n{name}")
        for label, ok in (await _phase(engine, cancel=cancel)).items():
            checks[f"{name}: {label}"] = ok

    print("\n--- Checklist ---")
    all_pass = True
    for label, ok in checks.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        all_pass = all_pass and ok
    print("\n" + ("=" * 20 + " PASS " + "=" * 20 if all_pass else "=" * 20 + " FAIL " + "=" * 20))
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
