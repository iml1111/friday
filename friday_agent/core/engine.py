"""FridayAgent — external entry point that runs one turn of the agent loop.

Holds the provider, tools, and call configuration, and exposes step(state): an
async generator that yields each Message produced during the turn (assistant
response, then each tool_result) and finally yields exactly one sentinel —
the next LoopState (loop may continue) or Terminal (loop has ended). The caller
drives the turn loop by calling step() until the sentinel is a Terminal.
"""
from __future__ import annotations

from collections import Counter
from typing import AsyncGenerator

from friday_agent.api.provider import LLMConfig, LLMProvider
from friday_agent.context.compact import compact_conversation, create_compact_summary_message
from friday_agent.memory.prompt import build_memory_section
from friday_agent.memory.store import FileMemoryStore, MemoryStore
from friday_agent.core.loop import run_one_turn
from friday_agent.core.state import LoopState, Terminal
from friday_agent.messages.normalize import normalize_for_api
from friday_agent.messages.types import Message
from friday_agent.tools.base import Tool
from friday_agent.tools.builtin import builtin_tools


class FridayAgent:
    """External entry point for the agent loop.

    Runs one turn per step() call; the caller drives the turn loop.
    A provider instance is required and injected directly; build one by
    instantiating a vendor adapter (e.g. AnthropicProvider(api_key=..., model=...)
    or OpenAIProvider(api_key=..., model=...)). The provider already holds its
    credentials, so FridayAgent takes no model or api_key argument.

    Args:
        provider: LLM backend instance (LLMProvider implementation). Required.
        tools: Available tools.
        system_prompt: Base system prompt text.
        config: Vendor call configuration, passed directly (e.g. AnthropicConfig).
                Defaults to the provider's default config (provider.config_type())
                when None.
        max_concurrency: Maximum concurrent tool executions (default 10).

    Raises:
        ValueError: When config is given but its type does not match the
                    provider's vendor (provider.config_type).
    """

    def __init__(
        self,
        provider: LLMProvider,
        tools: list[Tool] | None = None,
        system_prompt: str = "",
        config: LLMConfig | None = None,
        max_concurrency: int = 10,
        memory: MemoryStore | None = None,
    ) -> None:
        # Confirm config type matches the provider; fall back to the provider's default if None.
        if config is None:
            config = provider.config_type()
        elif not isinstance(config, provider.config_type):
            raise ValueError(
                f"FridayAgent: provider({type(provider).__name__}) expects "
                f"{provider.config_type.__name__} but received "
                f"{type(config).__name__}. Match the config type to the model vendor."
            )

        self._memory = memory if memory is not None else FileMemoryStore()
        caller_tools = tools if tools is not None else []
        assembled = [*caller_tools, *builtin_tools(), *self._memory.tools()]
        dups = sorted(n for n, c in Counter(t.name for t in assembled).items() if c > 1)
        if dups:
            raise ValueError(
                f"FridayAgent: duplicate tool names {dups}. TodoWrite and the "
                f"active MemoryStore's tools are SDK-managed and always registered; remove "
                f"the colliding tool(s) or override the store's tools()."
            )
        self._provider = provider
        self._tools = assembled
        self._system_prompt = system_prompt
        self._config = config
        self._max_concurrency = max_concurrency

    async def step(self, state: LoopState) -> AsyncGenerator[Message | LoopState | Terminal, None]:
        """Run one turn, streaming each Message as run_one_turn produces it.

        Yields every Message emitted during the turn (assistant response, then each
        tool_result) immediately, then yields exactly one final sentinel: the next
        LoopState (loop may continue) or a Terminal (loop ended).
        Thin passthrough over run_one_turn bound to this engine's provider/tools/config.

        The state may have been serialized and restored across containers, so this is
        the sole entry point for both starting and resuming. Distributed resume is
        unchanged: serialize the final LoopState directly.

        Raises:
            ContextOverflowError: propagated from run_one_turn during iteration when the
                provider rejects the messages as too long. The caller compacts via
                engine.compact(state) and retries.
        """
        # Rebuilt every turn from the store. In the distributed model the caller
        # reconstructs the engine each turn, so caching this would never be reused;
        # rebuilding keeps single-process behavior identical (index fresh per turn).
        memory_section = await build_memory_section(self._memory)
        effective_prompt = (
            f"{self._system_prompt}\n\n{memory_section}"
            if self._system_prompt
            else memory_section
        )
        tool_schemas = [tool.get_tool_schema() for tool in self._tools]
        async for item in run_one_turn(
            provider=self._provider,
            tools=self._tools,
            tool_schemas=tool_schemas,
            state=state,
            system_prompt=effective_prompt,
            config=self._config,
            max_concurrency=self._max_concurrency,
        ):
            yield item

    async def compact(self, state: LoopState) -> LoopState:
        """Summarize the entire conversation into one summary message and return a smaller LoopState.

        Recovery entry point for context overflow: when a turn cannot fit the
        model's context window, call compact(state) to replace all of
        state.messages with a single summary message, then retry. (After the
        caller-owned-compaction refactor, step() surfaces this as a raised
        ContextOverflowError.) turn_count is preserved for observability.
        """
        api_messages = normalize_for_api(state.messages)
        summary_text = await compact_conversation(
            provider=self._provider,
            messages=api_messages,
        )
        summary_message = create_compact_summary_message(summary_text)
        return LoopState(
            messages=[summary_message],
            turn_count=state.turn_count,
            todos=state.todos,
        )
