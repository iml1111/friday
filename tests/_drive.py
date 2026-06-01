"""Test-side driver: loops FridayAgent.step() to completion, recovering from overflow.

Consumer-side replacement for the removed query(). Lives in tests/ (not the
library) to prove that both the turn loop AND context recovery are trivial caller
loops. Yields each turn's Messages, then a final Terminal. On ContextOverflowError
it compacts and retries — the canonical caller pattern.
"""
from __future__ import annotations

from typing import AsyncGenerator

from friday_agent.api.provider import ContextOverflowError, LLMConfig, LLMProvider
from friday_agent.core.engine import FridayAgent
from friday_agent.core.state import LoopState, Terminal
from friday_agent.messages.types import Message
from friday_agent.tools.base import Tool


async def drive(
    *,
    provider: LLMProvider,
    tools: list[Tool] | None = None,
    messages: list[Message],
    system_prompt: str = "",
    config: LLMConfig | None = None,
    max_concurrency: int = 10,
) -> AsyncGenerator[Message | Terminal, None]:
    """Drive FridayAgent.step() to completion, compacting on context overflow.

    Yields every Message produced across turns, then yields the final Terminal.
    """
    engine = FridayAgent(
        provider=provider,
        tools=tools if tools is not None else [],
        system_prompt=system_prompt,
        config=config,
        max_concurrency=max_concurrency,
    )
    state = LoopState(messages=list(messages))
    while True:
        outcome: "LoopState | Terminal | None" = None
        try:
            async for item in engine.step(state):
                if isinstance(item, (LoopState, Terminal)):
                    outcome = item
                else:
                    yield item
        except ContextOverflowError:
            state = await engine.compact(state)
            continue
        if isinstance(outcome, Terminal):
            yield outcome
            return
        state = outcome


async def collect_turn(
    engine: FridayAgent, state: LoopState
) -> "tuple[list[Message], LoopState | Terminal]":
    """Drain one engine.step() turn → (messages, final sentinel). Test convenience.

    Replaces the removed StepOutcome for call sites that inspect a turn after it
    completes rather than rendering messages live.
    """
    messages: list[Message] = []
    outcome: "LoopState | Terminal | None" = None
    async for item in engine.step(state):
        if isinstance(item, (LoopState, Terminal)):
            outcome = item
        else:
            messages.append(item)
    assert outcome is not None, "step() must yield a LoopState or Terminal sentinel"
    return messages, outcome
