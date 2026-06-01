"""verify_p2.py — Single Tool Cycle verification against the real API.

Checks that a single ExampleTool call completes the full cycle:
tool invoked → tool_result fed back → final text response → Terminal(reason="completed").

Usage:
    LLM_MODEL=<model-id> python scripts/verify_p2.py

Cost guardrail: max_tokens=512; caller-side turn cap=5.
"""
from __future__ import annotations

import asyncio
import os
import sys

from _env import create_provider, resolve_api_key
from friday_agent.core.engine import FridayAgent
from friday_agent.core.state import Checkpoint, LoopState, Terminal
from friday_agent.messages.types import Message, create_user_message
from friday_agent.tools.builtin.example_tool import ExampleTool


# ── format helpers ──────────────────────────────────────────────────────────

def _print_message(msg: Message) -> None:
    """Print a message in human-readable form."""
    for block in msg.content:
        if block.type == "text":
            print(f"  [ASSISTANT TEXT] {block.text[:300]}")
        elif block.type == "tool_use":
            print(f"  [TOOL CALL] {block.name}({block.input})")
        elif block.type == "tool_result":
            preview = str(block.content)[:200]
            error_flag = " (ERROR)" if block.is_error else ""
            print(f"  [TOOL RESULT{error_flag}] tool_use_id={block.tool_use_id} → {preview}")


# ── verification main ────────────────────────────────────────────────────────

async def main() -> int:
    """Run single tool cycle verification. Returns 0 on PASS, 1 on FAIL."""
    print("=" * 60)
    print("Verification: Single Tool Cycle (Real API)")
    print("=" * 60)

    model = os.environ.get("LLM_MODEL", "")
    if not model:
        sys.exit("Set the LLM_MODEL environment variable to a real model ID.")
    tools = [ExampleTool()]
    provider = create_provider(model, api_key=resolve_api_key(model))
    config = provider.config_type(max_tokens=512)
    system_prompt = (
        "You are a helpful assistant. "
        "When asked to process text, use ExampleTool exactly ONCE with the provided payload. "
        "After receiving the tool result, summarize it in one sentence and stop."
    )

    engine = FridayAgent(
        provider=provider,
        tools=tools,
        system_prompt=system_prompt,
        config=config,
    )

    user_message = "Use ExampleTool to process the text 'hello world'. Then tell me the result."

    print(f"\nUser: {user_message}\n")

    _CAP = 5
    state = LoopState(messages=[create_user_message(user_message)])
    collected_messages: list[Message] = []
    terminal: Terminal | None = None
    turns = 0
    while True:
        outcome = None
        async for item in engine.step(state):
            if isinstance(item, (Checkpoint, Terminal)):
                outcome = item
            else:
                collected_messages.append(item)
        turns += 1
        if isinstance(outcome, Terminal):
            terminal = outcome
            break
        state = outcome.state
        if turns >= _CAP:
            break

    print("--- Messages received ---")
    tool_run = False
    tool_result_received = False
    final_text = None

    for msg in collected_messages:
        _print_message(msg)
        for block in msg.content:
            if block.type == "tool_use" and block.name == "ExampleTool":
                tool_run = True
                print(f"    → ExampleTool called with payload={block.input.get('payload')!r}")
            if block.type == "tool_result":
                tool_result_received = True
                print(f"    → tool_result content: {str(block.content)[:100]!r}")
            if block.type == "text" and block.text:
                final_text = block.text

    print(f"\n--- Terminal ---")
    print(f"  reason    : {terminal.reason if terminal else f'(turn cap {turns} reached)'}")
    print(f"  turns_run : {turns}")
    if terminal and terminal.error:
        print(f"  error     : {terminal.error}")

    print("\n--- Checklist ---")
    checks = {
        "ExampleTool was called": tool_run,
        "tool_result was fed back": tool_result_received,
        "final text response produced": bool(final_text),
        "Terminal reason == 'completed'": terminal is not None and terminal.reason == "completed",
    }

    all_pass = True
    for label, ok in checks.items():
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {label}")
        if not ok:
            all_pass = False

    print("\n" + ("=" * 20 + " PASS " + "=" * 20 if all_pass else "=" * 20 + " FAIL " + "=" * 20))
    return 0 if all_pass else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
