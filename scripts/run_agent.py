"""run_agent.py — Interactive REPL for the agent loop.

Run the agent loop interactively without writing any import code.
Each input drives step() to completion over the accumulated history to maintain multi-turn context.

Usage:
    python scripts/run_agent.py

Model is set by the _MODEL constant near the top of this file.

Commands:
    exit / quit / Ctrl-D   quit the REPL
    (blank lines are ignored)

ExampleTool is wired in by default so you can observe the
tool_use → tool_result → response cycle immediately.
ExampleTool echoes its input back as 'processed: X'. Replace the _dispatch
in friday_agent/tools/builtin/example_tool.py to build real tools.

Cost guardrail: max_tokens=1024; caller-side turn cap = 30 per input to limit spend.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Add the repo root to sys.path so friday_agent can be imported without an editable install.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_MODEL="claude-sonnet-4-6"

from _env import create_provider, resolve_api_key
from friday_agent.api.provider import ContextOverflowError
from friday_agent.core.engine import QueryEngine
from friday_agent.core.state import Checkpoint, LoopState, Terminal
from friday_agent.messages.types import Message, create_user_message
from friday_agent.tools.builtin.example_tool import ExampleTool

_MAX_TURNS = 30      # per-input loop cap to prevent runaway tool calls
_MAX_TOKENS = 1024   # max output tokens per response
_SYSTEM_PROMPT = (
    "You are a helpful assistant. "
    "When the user asks you to process some text, call ExampleTool with that text "
    "as the payload, then report the tool's result."
)


def _print_new_messages(messages: list[Message]) -> None:
    """Print messages emitted by the loop this turn (assistant, tool)."""
    for msg in messages:
        for block in msg.content:
            if block.type == "text":
                if block.text:
                    print(f"[assistant] {block.text}")
            elif block.type == "tool_use":
                print(f"  [tool call] {block.name}({block.input})")
            elif block.type == "tool_result":
                flag = " (error)" if block.is_error else ""
                print(f"  [tool result{flag}] {block.content}")


async def main() -> int:
    provider = create_provider(_MODEL, api_key=resolve_api_key(_MODEL))
    engine = QueryEngine(
        provider=provider,
        tools=[ExampleTool()],
        system_prompt=_SYSTEM_PROMPT,
        config=provider.config_type(max_tokens=_MAX_TOKENS),
    )

    print(f"friday-agent REPL — model={_MODEL}  (tools: ExampleTool)")
    print("Enter a prompt. To quit: exit / quit / Ctrl-D\n")

    history: list[Message] = []
    while True:
        try:
            line = input("> ").strip()
        except EOFError:
            print()
            break
        if not line:
            continue
        if line in ("exit", "quit"):
            break

        history.append(create_user_message(line))
        state = LoopState(messages=list(history))
        collected: list[Message] = []
        terminal: Terminal | None = None
        turns = 0
        try:
            while True:
                outcome = None
                try:
                    async for item in engine.step(state):
                        if isinstance(item, (Checkpoint, Terminal)):
                            outcome = item
                        else:
                            _print_new_messages([item])   # per-message live render
                            collected.append(item)
                except ContextOverflowError:
                    print("  ── [context overflow → compacting] ──")
                    state = await engine.compact(state)
                    continue
                turns += 1
                if isinstance(outcome, Terminal):
                    terminal = outcome
                    break
                state = outcome.state
                if turns >= _MAX_TURNS:
                    break
        except Exception as exc:
            # Catch API errors so the REPL stays alive; roll back the failed turn.
            print(f"  [error] {type(exc).__name__}: {exc}")
            history.pop()
            continue

        # Messages were printed per-turn above; commit them to history once here so a
        # mid-loop error (handled above with history.pop()) rolls back the whole turn cleanly.
        history.extend(collected)

        if terminal is None:
            print(f"  ── [loop stopped: turn cap {_MAX_TURNS} reached] ──")
        elif terminal.reason != "completed":
            print(f"  ── [loop ended: {terminal.reason}] ──")
        print()

    print("bye.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
