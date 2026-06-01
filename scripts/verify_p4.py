"""verify_p4.py — Real backend end-to-end verification.

Runs two sub-scenarios against the actual LLM API using the routed provider:
  - Sub-scenario A: single tool cycle (ExampleTool called, result fed back, final text produced).
  - Sub-scenario B: external compact recovery across a serialize boundary.
    NOTE: Because the library no longer has a token threshold, this verifies
    the recovery MECHANISM via an explicit engine.compact() call rather than
    triggering a real context-overflow 413 (which would require ~200K input
    tokens and would be prohibitively costly).

Also asserts that the routed provider is an instance of the LLMProvider ABC,
confirming the adapter boundary is correctly implemented.

Usage:
    LLM_MODEL=<model-id> python scripts/verify_p4.py

Cost guardrail: ~3-5 total API calls (sub-A ~2, sub-B ~4-5); max_tokens=512; caller-side turn cap=5 (sub-A), 6 (sub-B).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

_MODEL = os.environ.get("LLM_MODEL", "")
if not _MODEL:
    sys.exit("Set the LLM_MODEL environment variable to a real model ID.")

from _env import create_config, create_provider, resolve_api_key
from friday_agent.api.provider import LLMProvider
from friday_agent.core.engine import FridayAgent
from friday_agent.core.state import LoopState, Terminal
from friday_agent.messages.types import create_user_message
from friday_agent.tools.base import Tool, ToolResult
from friday_agent.tools.builtin.example_tool import ExampleTool

from pydantic import BaseModel, Field


# ── token-accumulation tool (same pattern as verify_p3.py) ──────────────────

class ChunkInput(BaseModel):
    query: str = Field(description="Query string")


class BigChunkTool(Tool):
    """Returns a large fixed text block on every call to accumulate tokens quickly."""

    name = "BigChunkTool"

    # ~600 chars ≈ 150 tokens per call
    _CHUNK = (
        "Here is a large block of text data for accumulation purposes. "
        "The quick brown fox jumps over the lazy dog. "
        "Pack my box with five dozen liquor jugs. "
        "How vividly the jagged peaks of the frozen mountain towered above. "
        "Sphinx of black quartz, judge my vow. "
        "The five boxing wizards jump quickly over the lazy fence. "
        "Bright copper kettles and warm woolen mittens are among favourite things. "
        "Consider the computational complexity of parsing unstructured natural language. "
        "Memory management patterns in long-running agent loops require careful design. "
        "Context window management ensures stable operation across many conversation turns. "
    )

    def input_schema(self) -> type[BaseModel]:
        return ChunkInput

    def is_read_only(self, input_data: dict) -> bool:
        return True

    def is_concurrency_safe(self, input_data: dict) -> bool:
        return True

    async def call(self, args: dict) -> ToolResult:
        parsed = ChunkInput(**args)
        result = f"Data for '{parsed.query}': {self._CHUNK}"
        return ToolResult(data=result)


# ── sub-scenario A: single tool cycle ───────────────────────────────────────

async def run_p4a() -> tuple[bool, str]:
    """Single tool cycle sub-scenario. Returns (passed, detail_message)."""
    provider = create_provider(_MODEL, api_key=resolve_api_key(_MODEL))

    if not isinstance(provider, LLMProvider):
        return False, "routed provider is not an instance of LLMProvider ABC"

    tools = [ExampleTool()]
    config = create_config(_MODEL, max_tokens=512)
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

    _CAP = 5
    state = LoopState(messages=[create_user_message(
        "Use ExampleTool to process the text 'p4 integration test'. Then tell me the result."
    )])
    msgs = []
    terminal = None
    turns = 0
    while True:
        outcome = None
        async for item in engine.step(state):
            if isinstance(item, (LoopState, Terminal)):
                outcome = item
            else:
                msgs.append(item)
        turns += 1
        if isinstance(outcome, Terminal):
            terminal = outcome
            break
        state = outcome
        if turns >= _CAP:
            break

    tool_run = any(
        block.type == "tool_use" and block.name == "ExampleTool"
        for msg in msgs for block in msg.content
    )
    tool_result_received = any(
        block.type == "tool_result"
        for msg in msgs for block in msg.content
    )
    has_final_text = any(
        block.type == "text" and block.text
        for msg in msgs for block in msg.content
    )

    if not (tool_run and tool_result_received and has_final_text and terminal is not None and terminal.reason == "completed"):
        return False, (
            f"sub-A failed: tool_run={tool_run}, result={tool_result_received}, "
            f"text={has_final_text}, terminal={terminal.reason if terminal else 'cap'}"
        )

    return True, "tool called, result fed back, final text produced, terminal=completed"


# ── sub-scenario B: external compact recovery across serialize boundary ───────

async def run_p4b() -> tuple[bool, str]:
    """External compact recovery across a serialize boundary. Returns (passed, detail)."""
    provider = create_provider(_MODEL, api_key=resolve_api_key(_MODEL))
    tools = [BigChunkTool()]
    config = create_config(_MODEL, max_tokens=512)
    system_prompt = (
        "You are a data gathering assistant. Call BigChunkTool every turn with a "
        "different query string. Never say you are done; just keep calling the tool."
    )
    engine = FridayAgent(provider=provider, tools=tools, system_prompt=system_prompt, config=config)

    _CAP = 6
    state = LoopState(messages=[create_user_message(
        "Start gathering data. Call BigChunkTool with 'p4 round 1', then 'p4 round 2', and so on. Do NOT stop."
    )])
    turns = 0
    accumulated = state
    while turns < _CAP:
        outcome = None
        async for item in engine.step(state):
            if isinstance(item, (LoopState, Terminal)):
                outcome = item
        turns += 1
        if isinstance(outcome, Terminal):
            break
        state = outcome
        accumulated = state

    compacted = await engine.compact(accumulated)
    summary_ok = len(compacted.messages) == 1 and compacted.messages[0].is_compact_summary

    # Serialize boundary: compacted state survives JSON round-trip.
    blob = json.dumps(compacted.to_dict(), ensure_ascii=False)
    restored = LoopState.from_dict(json.loads(blob))
    serde_ok = len(restored.messages) == 1 and restored.messages[0].is_compact_summary

    # Continuation after resume.
    cont_outcome = None
    async for item in engine.step(restored):
        if isinstance(item, (LoopState, Terminal)):
            cont_outcome = item
    continued_ok = isinstance(cont_outcome, (LoopState, Terminal))

    if not (summary_ok and serde_ok and continued_ok):
        return False, f"sub-B: summary_ok={summary_ok}, serde_ok={serde_ok}, continued_ok={continued_ok}"
    return True, "compact → serialize → resume → step succeeded"


# ── main ─────────────────────────────────────────────────────────────────────

async def main() -> int:
    """Run all sub-scenarios. Returns 0 if all pass, 1 if any fail."""
    print("=" * 60)
    print("Verification: Real Backend End-to-End (routed provider)")
    print("=" * 60)

    provider_check = isinstance(create_provider(_MODEL, api_key=resolve_api_key(_MODEL)), LLMProvider)
    print(f"\n  routed provider instanceof LLMProvider: {provider_check}")
    print("  (LLMProvider ABC is the sole adapter-swap boundary)\n")

    print("--- Sub-scenario A: single tool cycle ---")
    p4a_pass, p4a_detail = await run_p4a()
    status_a = "PASS" if p4a_pass else "FAIL"
    print(f"  [{status_a}] {p4a_detail}")

    print("\n--- Sub-scenario B: external compact recovery across serialize boundary ---")
    p4b_pass, p4b_detail = await run_p4b()
    status_b = "PASS" if p4b_pass else "FAIL"
    print(f"  [{status_b}] {p4b_detail}")

    all_pass = p4a_pass and p4b_pass and provider_check
    print("\n--- Checklist ---")
    checks = {
        "routed provider implements LLMProvider ABC": provider_check,
        "sub-A (single tool cycle) passed": p4a_pass,
        "sub-B (external compact recovery) passed": p4b_pass,
    }
    for label, ok in checks.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")

    print("\n" + ("=" * 20 + " PASS " + "=" * 20 if all_pass else "=" * 20 + " FAIL " + "=" * 20))
    return 0 if all_pass else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
