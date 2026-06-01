"""verify_todo.py — Real-API smoke test for the todo reminder injection.

Confirms the one thing fake-provider tests cannot: that the backend accepts a
user turn carrying a text block appended AFTER tool_result blocks (the reminder
joined onto a tool_result turn).

Usage:
    LLM_MODEL=<model-id> python scripts/verify/verify_todo.py

Cost guardrail: max_tokens=512; caller-side turn cap=6.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # scripts/ → import _env
from _env import create_config, create_provider, resolve_api_key
from friday_agent.core.engine import FridayAgent
from friday_agent.core.state import LoopState, Terminal
from friday_agent.messages.types import Message, create_user_message
from friday_agent.tools.builtin.example_tool import ExampleTool
from friday_agent.tools.builtin.todo_write import TodoWrite


class _Recording:
    """Duck-typed LLMProvider wrapper that captures each complete() call's messages."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.config_type = inner.config_type
        self.received_messages: list[list[dict]] = []

    async def complete(self, messages, system_prompt, tools, config):
        self.received_messages.append(messages)
        return await self._inner.complete(
            messages=messages, system_prompt=system_prompt, tools=tools, config=config
        )


def _reminder_after_tool_result(api_messages: list[dict]) -> bool:
    """True if some user message contains a tool_result block followed by a
    <system-reminder> text block (the exact shape we need the API to accept)."""
    for m in api_messages:
        if m.get("role") != "user":
            continue
        types = [b.get("type") for b in m["content"]]
        if "tool_result" not in types:
            continue
        for b in m["content"]:
            if b.get("type") == "text" and "<system-reminder>" in (b.get("text") or ""):
                return True
    return False


async def main() -> int:
    print("=" * 60)
    print("Verification: Todo reminder injection (Real API)")
    print("=" * 60)

    model = os.environ.get("LLM_MODEL", "")
    if not model:
        sys.exit("Set the LLM_MODEL environment variable to a real model ID.")

    provider = _Recording(create_provider(model, api_key=resolve_api_key(model)))
    config = create_config(model, max_tokens=512)
    system_prompt = (
        "You are a helpful assistant doing multi-step work. "
        "FIRST call TodoWrite with a 2-item list (one in_progress, one pending). "
        "THEN call ExampleTool once to process the text 'hello'. "
        "THEN mark both todos completed via TodoWrite and give a one-sentence summary. Stop."
    )
    engine = FridayAgent(provider=provider, tools=[TodoWrite(), ExampleTool()],
                         system_prompt=system_prompt, config=config)

    user_message = "Please complete the two-step task and track it with todos."
    print(f"\nUser: {user_message}\n")

    _CAP = 6
    state = LoopState(messages=[create_user_message(user_message)])
    collected: list[Message] = []
    terminal: Terminal | None = None
    todos_seen_nonempty = False
    turns = 0
    while True:
        outcome = None
        async for item in engine.step(state):
            if isinstance(item, (LoopState, Terminal)):
                outcome = item
            else:
                collected.append(item)
        turns += 1
        if isinstance(outcome, Terminal):
            terminal = outcome
            break
        state = outcome
        if state.todos:
            todos_seen_nonempty = True
        if turns >= _CAP:
            break

    todowrite_called = any(
        b.type == "tool_use" and b.name == "TodoWrite" for m in collected for b in m.content
    )
    reminder_shape_sent = any(_reminder_after_tool_result(msgs) for msgs in provider.received_messages)

    print(f"\n--- Terminal ---")
    print(f"  reason    : {terminal.reason if terminal else f'(turn cap {turns} reached)'}")
    print(f"  api_calls : {len(provider.received_messages)}")
    if terminal and terminal.error:
        print(f"  error     : {terminal.error}")

    print("\n--- Checklist ---")
    checks = {
        "TodoWrite was called": todowrite_called,
        "todos became non-empty in state": todos_seen_nonempty,
        "reminder sent appended-after-tool_result (>=1 call)": reminder_shape_sent,
        "API accepted reminder turn (reason == 'completed')":
            terminal is not None and terminal.reason == "completed",
    }
    all_pass = True
    for label, ok in checks.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        all_pass = all_pass and ok

    print("\n" + ("=" * 20 + " PASS " + "=" * 20 if all_pass else "=" * 20 + " FAIL " + "=" * 20))
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
