"""verify_cache.py — Always-on prompt caching verification against the real API.

Drives the real FridayAgent for two sequential turns over a byte-stable,
oversized system+tools prefix, capturing each completion's TokenUsage via a
thin recording wrapper around the provider. Asserts the behavior predicted by
AnthropicProvider._apply_cache_control:

  * Turn 1 (cold): cache_creation_input_tokens > 0, cache_read_input_tokens == 0
  * Turn 2 (warm): cache_read_input_tokens > 0  (the prefix written on turn 1
    is read back at ~0.1x instead of reprocessed at 1x)

A per-run nonce embedded in the system prompt makes turn 1 cold even on rapid
re-runs (defeats the 5-minute server TTL left by a previous run), while both
turns share the identical prompt so turn 2 hits.

Usage:
    LLM_MODEL=claude-haiku-4-5 python scripts/verify/verify_cache.py

Cost guardrail: max_tokens=256; exactly two completion calls; ~12K-token prefix.
Anthropic-only — it verifies the explicit cache_control breakpoints. For gpt-*
models caching is automatic (no _apply_cache_control), so the script declines.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # scripts/ -> import _env
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root -> friday_agent (when not pip-installed)
from _env import create_config, create_provider, resolve_api_key
from friday_agent.api.provider import AssistantResponse, LLMProvider, TokenUsage
from friday_agent.core.engine import FridayAgent
from friday_agent.core.state import LoopState, Terminal
from friday_agent.messages.types import Message, create_user_message
from friday_agent.tools.builtin.example_tool import ExampleTool


# A unique nonce per run guarantees turn 1 cannot hit a cache a previous run
# left behind; the SAME nonce is reused on both turns so turn 2 still matches.
_RUN_NONCE = uuid.uuid4().hex

# Static filler that pushes the cacheable prefix above the per-model minimum
# (Opus/Haiku 4.x = 4096 tokens, Sonnet 4.6 = 2048) so the breakpoint engages.
_FILLER = "\n".join(
    f"Reference line {i:04d}: friday prompt-caching verification corpus; this "
    f"static sentence pads the cacheable prefix above the per-model minimum so "
    f"the ephemeral breakpoint engages deterministically on every Claude model."
    for i in range(300)
)
SYSTEM_PROMPT = (
    f"You are a prompt-caching verification assistant (run id {_RUN_NONCE}).\n"
    "Be terse. When asked to process text, call ExampleTool exactly once and "
    "then summarize the tool result in one short sentence.\n\n"
    f"{_FILLER}"
)


class _RecordingProvider(LLMProvider):
    """Transparent wrapper: records each call's TokenUsage while delegating the
    completion (and thus _apply_cache_control) to the real provider verbatim."""

    def __init__(self, inner: LLMProvider) -> None:
        self._inner = inner
        self.config_type = inner.config_type  # mirror so FridayAgent validation passes
        self.calls: list[TokenUsage] = []

    async def complete(self, messages, system_prompt, tools, config) -> AssistantResponse:
        resp = await self._inner.complete(messages, system_prompt, tools, config)
        self.calls.append(resp.usage)
        return resp


async def _run_turn(engine: FridayAgent, state: LoopState):
    """Drive one engine.step() to its sentinel; return (sentinel, [messages])."""
    collected: list[Message] = []
    outcome: LoopState | Terminal | None = None
    async for item in engine.step(state):
        if isinstance(item, (LoopState, Terminal)):
            outcome = item
        else:
            collected.append(item)
    return outcome, collected


def _diag_turn(n: int, outcome, collected: list[Message]) -> None:
    """Print one turn's sentinel kind (+ Terminal reason/error) and block types."""
    kinds = [b.type for m in collected for b in m.content]
    tag = type(outcome).__name__
    if isinstance(outcome, Terminal):
        tag += f"(reason={outcome.reason}, error={outcome.error!r})"
    print(f"  turn {n}: {tag}; blocks={kinds}")


def _fmt(u: TokenUsage) -> str:
    return (
        f"input={u.input_tokens:>6}  output={u.output_tokens:>4}  "
        f"cache_creation={u.cache_creation_input_tokens:>6}  "
        f"cache_read={u.cache_read_input_tokens:>6}"
    )


async def main() -> int:
    print("=" * 66)
    print("Verification: Always-on Prompt Caching (_apply_cache_control, Real API)")
    print("=" * 66)

    model = os.environ.get("LLM_MODEL", "")
    if not model:
        sys.exit("Set LLM_MODEL to a real Claude model ID (e.g. claude-haiku-4-5).")
    if not model.startswith("claude-"):
        print(f"\nNOTE: {model!r} is not an Anthropic model.")
        print("_apply_cache_control only runs in the Anthropic adapter; OpenAI caches")
        print("automatically with no explicit breakpoints. Rerun with a claude-* model.")
        return 1

    # Optional: enable extended thinking via THINK_BUDGET=<tokens> to confirm the
    # cache structure survives reasoning (thinking is OFF by default in friday).
    think_budget = int(os.environ.get("THINK_BUDGET", "0") or "0")
    thinking_on = think_budget > 0
    cfg_kwargs: dict = {"max_tokens": (think_budget + 512) if thinking_on else 256}
    if thinking_on:
        cfg_kwargs.update(thinking_enabled=True, thinking_budget=think_budget)

    recorder = _RecordingProvider(create_provider(model, api_key=resolve_api_key(model)))
    config = create_config(model, **cfg_kwargs)
    engine = FridayAgent(
        provider=recorder,
        tools=[ExampleTool()],
        system_prompt=SYSTEM_PROMPT,
        config=config,
    )

    print(f"\nmodel         : {model}")
    print(f"run nonce     : {_RUN_NONCE}")
    print(f"thinking      : {'ON (budget=' + str(think_budget) + ')' if thinking_on else 'OFF'}")
    print(f"system prompt : {len(SYSTEM_PROMPT):,} chars (base; engine also appends guidance+memory)")
    print("Driving 2 sequential turns over an identical system+tools prefix...\n")

    # Turn 1 — cold prefix (nothing cached yet for this nonce).
    state1 = LoopState(messages=[create_user_message(
        "Use ExampleTool to process the text 'caching probe', then tell me the result."
    )])
    outcome1, collected1 = await _run_turn(engine, state1)
    _diag_turn(1, outcome1, collected1)

    # Build a well-formed second turn whether or not the model used the tool.
    if isinstance(outcome1, LoopState):
        state2 = outcome1  # tool path: returned state already has par-valid history
    else:  # end_turn path: append the assistant reply + a fresh user turn
        state2 = LoopState(messages=[
            *state1.messages, *collected1,
            create_user_message("Reply with the single word: done."),
        ])

    # Turn 2 — warm prefix (must read back what turn 1 wrote).
    outcome2, collected2 = await _run_turn(engine, state2)
    _diag_turn(2, outcome2, collected2)

    thinking_seen = any(
        b.type == "thinking"
        for msgs in (collected1, collected2) for m in msgs for b in m.content
    )

    print("--- Per-call token usage ---")
    for i, u in enumerate(recorder.calls, 1):
        print(f"  call {i}: {_fmt(u)}")
    if thinking_on:
        print(f"  thinking block actually generated in responses: {thinking_seen}")

    if len(recorder.calls) < 2:
        print("\n[FAIL] fewer than 2 completion calls captured — cannot verify cross-turn cache.")
        print("=" * 28 + " FAIL " + "=" * 28)
        return 1

    c1, c2 = recorder.calls[0], recorder.calls[1]
    checks = {
        "turn 1 wrote cache (cache_creation > 0)": c1.cache_creation_input_tokens > 0,
        "turn 1 was cold (cache_read == 0)": c1.cache_read_input_tokens == 0,
        "turn 2 read cache (cache_read > 0)": c2.cache_read_input_tokens > 0,
        "turn 2 read ~= turn 1 prefix (read >= 50% of turn-1 write)":
            c2.cache_read_input_tokens >= 0.5 * max(c1.cache_creation_input_tokens, 1),
    }

    print("\n--- Checklist ---")
    all_pass = True
    for label, ok in checks.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        all_pass &= ok

    if c1.cache_creation_input_tokens == 0:
        print("\n  hint: cache_creation==0 on turn 1 means the prefix stayed under this")
        print("  model's minimum cacheable size — raise the _FILLER line count.")

    saved = c2.cache_read_input_tokens
    print(f"\n  turn-2 cached-read tokens: {saved:,} "
          f"(billed ~0.1x vs 1x -> ~{saved * 0.9:,.0f} tokens' worth saved this turn)")

    print("\n" + ("=" * 28 + " PASS " + "=" * 28 if all_pass
                  else "=" * 28 + " FAIL " + "=" * 28))
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
