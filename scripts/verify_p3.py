"""verify_p3.py — Caller-Owned Compaction verification against the real API.

Verifies that, against the real API, a multi-turn conversation can be compacted
via engine.compact() and continue normally (the caller-owned recovery path).

NOTE: Because the library no longer has a token threshold, this script verifies
the recovery MECHANISM via an explicit engine.compact() call rather than
triggering a real context-overflow 413 (which would require ~200K input tokens
and would be prohibitively costly).

Usage:
    LLM_MODEL=<model-id> python scripts/verify_p3.py

Cost guardrail: max_tokens=512; caller-side turn cap=6.
"""
from __future__ import annotations

import asyncio
import os
import sys

_MODEL = os.environ.get("LLM_MODEL", "")
if not _MODEL:
    sys.exit("Set the LLM_MODEL environment variable to a real model ID.")

from _env import create_provider, resolve_api_key
from friday_agent.core.engine import FridayAgent
from friday_agent.core.state import Checkpoint, LoopState, Terminal
from friday_agent.messages.types import create_user_message
from friday_agent.tools.base import Tool, ToolResult

from pydantic import BaseModel, Field


# ── token-accumulation tool ─────────────────────────────────────────────────

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


# ── verification main ────────────────────────────────────────────────────────

async def main() -> int:
    print("=" * 60)
    print("Verification: Caller-Owned Compaction (Real API, in-process)")
    print("=" * 60)

    provider = create_provider(_MODEL, api_key=resolve_api_key(_MODEL))
    tools = [BigChunkTool()]
    config = provider.config_type(max_tokens=512)
    system_prompt = (
        "You are a data gathering assistant. Call BigChunkTool every turn with a "
        "different query string. Never say you are done; just keep calling the tool."
    )
    engine = FridayAgent(provider=provider, tools=tools, system_prompt=system_prompt, config=config)

    _CAP = 6
    state = LoopState(messages=[create_user_message(
        "Start gathering data. Call BigChunkTool with 'round 1', then 'round 2', and so on. Do NOT stop."
    )])
    turns = 0
    accumulated = state
    while turns < _CAP:
        outcome = None
        async for item in engine.step(state):
            if isinstance(item, (Checkpoint, Terminal)):
                outcome = item
        turns += 1
        if isinstance(outcome, Terminal):
            break
        state = outcome.state
        accumulated = state

    pre_count = len(accumulated.messages)
    print(f"\n  accumulated messages before compact = {pre_count}")

    compacted = await engine.compact(accumulated)

    print("--- compact() result ---")
    summary_ok = len(compacted.messages) == 1 and compacted.messages[0].is_compact_summary
    summary_text = " ".join(
        b.text or "" for b in compacted.messages[0].content if b.text
    ) if compacted.messages else ""
    print(f"  messages after compact = {len(compacted.messages)}")
    print(f"  summary preview        = {summary_text[:150]!r}")
    turn_count_ok = compacted.turn_count == accumulated.turn_count

    # Continuation: one more step on the compacted state must proceed without error.
    cont_outcome = None
    async for item in engine.step(compacted):
        if isinstance(item, (Checkpoint, Terminal)):
            cont_outcome = item
    continued_ok = isinstance(cont_outcome, (Checkpoint, Terminal))

    print("\n--- Checklist ---")
    checks = {
        "compact() returned a single summary message": summary_ok,
        "summary text is non-empty": bool(summary_text.strip()),
        "turn_count preserved across compact": turn_count_ok,
        "step() continued after compact": continued_ok,
    }
    all_pass = True
    for label, ok in checks.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        all_pass = all_pass and ok

    print("\n" + ("=" * 20 + " PASS " + "=" * 20 if all_pass else "=" * 20 + " FAIL " + "=" * 20))
    return 0 if all_pass else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
