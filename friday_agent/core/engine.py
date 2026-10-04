"""FridayAgent — external entry point that runs one turn of the agent loop.

Holds the provider, tools, and call configuration, and exposes step(state): an
async generator that yields each Message produced during the turn (assistant
response, then each tool_result) and finally yields exactly one sentinel —
the next LoopState (continue), Suspended (paused on deferred tool calls) or
Terminal (ended). The caller drives the turn loop by calling step() until the
sentinel is a Terminal.
"""
from __future__ import annotations

from collections import Counter
from typing import AsyncGenerator, Awaitable, Callable

from friday_agent.api.prompts import assemble_system_prompt
from friday_agent.api.provider import LLMConfig, LLMProvider
from friday_agent.context.compact import SUMMARIZER_SYSTEM_PROMPT, compact_conversation, create_compact_summary_message
from friday_agent.memory.store import (
    MEMORY_INSTRUCTIONS,
    MemoryStore,
    build_memory_reminder,
)
from friday_agent.core.loop import pending_tool_uses, run_one_turn
from friday_agent.core.state import LoopState, PendingToolUseError, Suspended, Terminal
from friday_agent.messages.normalize import normalize_for_api
from friday_agent.messages.types import Message, wrap_system_reminder
from friday_agent.tools.base import Tool
from friday_agent.tools.builtin import builtin_tools

# A per-turn section: rendered from the turn's input state on every step();
# its output rides the trailing user message as a <system-reminder> (never
# persisted, never part of the cached prefix).
TurnSection = Callable[[LoopState], Awaitable[str]]


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
        memory: Optional MemoryStore. When None (the default), the memory
                subsystem is not mounted: no memory tools, no MEMORY_INSTRUCTIONS
                system section, no per-turn index reminder. Pass a store
                (e.g. FileMemoryStore()) to opt in.
        compact_instructions: Domain requirements folded into the compaction
                prompt used by compact(). system_prompt does not reach that call
                (summarization runs under its own summarizer system prompt), so
                this is the only way to steer what a summary must preserve.
                Empty (the default) leaves the base prompt untouched.
        turn_sections: Async callables rendered on every step() from the turn's
                input state. Each non-empty output is wrapped in a
                <system-reminder> and joined onto the trailing user message of
                the API view only (after the todo reminder and the memory
                index) — never persisted into LoopState, never part of the
                cached prefix. Use for content that changes during a session
                (current screen, progress); static content belongs in
                system_prompt. Empty strings are dropped; exceptions propagate.

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
        compact_instructions: str = "",
        turn_sections: list[TurnSection] | None = None,
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

        self._memory = memory
        caller_tools = tools if tools is not None else []
        memory_tools = self._memory.tools() if self._memory is not None else []
        assembled = [*caller_tools, *builtin_tools(), *memory_tools]
        dups = sorted(n for n, c in Counter(t.name for t in assembled).items() if c > 1)
        if dups:
            raise ValueError(
                f"FridayAgent: duplicate tool names {dups}. TodoWrite (and the "
                f"mounted MemoryStore's tools, when memory is passed) are SDK-managed; "
                f"remove the colliding tool(s) or override the store's tools()."
            )
        self._provider = provider
        self._tools = assembled
        self._system_prompt = system_prompt
        self._config = config
        self._max_concurrency = max_concurrency
        self._compact_instructions = compact_instructions
        self._turn_sections = list(turn_sections or [])

    def _effective_system_prompt(self) -> str:
        """Static system prefix: memory instructions (when mounted) -> domain prompt.

        Must stay byte-stable within a session — it heads the cached prefix, and
        compact(reuse_prefix=True) reproduces it to read the conversation cache.
        """
        memory_section = MEMORY_INSTRUCTIONS if self._memory is not None else ""
        return "\n\n".join(p for p in (memory_section, self._system_prompt) if p)

    def _tool_schemas(self) -> list[dict]:
        return [tool.get_tool_schema() for tool in self._tools]

    async def step(self, state: LoopState) -> AsyncGenerator[Message | LoopState | Suspended | Terminal, None]:
        """Run one turn, streaming each Message as run_one_turn produces it.

        Yields every Message emitted during the turn (assistant response, then each
        tool_result) immediately, then yields exactly one final sentinel: the next
        LoopState (continue), Suspended (paused on deferred tool calls — attach
        their results with resume(), then step() again), or a Terminal (ended;
        terminal.state is the state to keep).
        Thin passthrough over run_one_turn bound to this engine's provider/tools/config.

        The state may have been serialized and restored across containers, so this is
        the sole entry point for both starting and resuming. Distributed resume is
        unchanged: serialize the final LoopState directly.

        Raises:
            PendingToolUseError: the state still has unanswered tool_use blocks —
                checked before anything else (no section rendered, no request sent).
            ContextOverflowError: propagated from run_one_turn during iteration when the
                provider rejects the messages as too long. The caller compacts via
                engine.compact(state) and retries.
        """
        if pending := pending_tool_uses(state):
            raise PendingToolUseError([block.id or "" for block in pending])
        # System prefix: static pieces only, ordered generic -> specific
        # (memory instructions -> domain prompt) — must be byte-stable within a
        # session so the cache prefix survives. Per-turn content (the live memory
        # index, then turn_sections outputs) is rebuilt every turn and rides
        # messages[-1] as turn-local reminders instead — in the system prompt it
        # would invalidate the whole conversation cache.
        effective_prompt = self._effective_system_prompt()
        turn_reminders = (
            [await build_memory_reminder(self._memory)] if self._memory is not None else []
        )
        for section in self._turn_sections:
            text = await section(state)
            if text:
                turn_reminders.append(wrap_system_reminder(text))
        turn_reminders = [t for t in turn_reminders if t]
        async for item in run_one_turn(
            provider=self._provider,
            tools=self._tools,
            tool_schemas=self._tool_schemas(),
            state=state,
            system_prompt=effective_prompt,
            config=self._config,
            max_concurrency=self._max_concurrency,
            turn_reminders=turn_reminders,
        ):
            yield item

    async def compact(self, state: LoopState, *, reuse_prefix: bool = False) -> LoopState:
        """Summarize the entire conversation into one summary message and return a smaller LoopState.

        Recovery entry point for context overflow (and for proactive compaction):
        when a turn cannot fit the model's context window, call compact(state) to
        replace all of state.messages with a single summary message, then retry.
        turn_count and todos are preserved.

        By default the summarizer runs under SUMMARIZER_SYSTEM_PROMPT with no tools —
        compact_instructions (constructor) is the injection point for domain
        requirements about what the summary must preserve. turn_sections are never
        rendered here.

        Args:
            state: The state to compact.
            reuse_prefix: Send the summary call with exactly the system prompt and
                tool schemas step() sends, so the provider can serve the
                conversation from its prompt cache instead of writing it again. A
                reply that calls a tool or lacks a usable <summary> is retried once
                with tools=[]. Best for proactive compaction: the agent's prefix adds
                tokens, so during overflow recovery the summary call itself can
                overflow; and with extended thinking enabled in this agent's config,
                the summary call (thinking off) cannot reuse the message cache.

        Raises:
            PendingToolUseError: the state still has unanswered tool_use blocks
                (its summary call would send them unpaired).
        """
        if pending := pending_tool_uses(state):
            raise PendingToolUseError([block.id or "" for block in pending])
        if reuse_prefix:
            system_prompt = str(assemble_system_prompt(self._effective_system_prompt()))
            tool_schemas = self._tool_schemas()
        else:
            system_prompt, tool_schemas = SUMMARIZER_SYSTEM_PROMPT, []
        summary_text = await compact_conversation(
            provider=self._provider,
            messages=normalize_for_api(state.messages),
            extra_instructions=self._compact_instructions,
            system_prompt=system_prompt,
            tools=tool_schemas,
        )
        summary_message = create_compact_summary_message(summary_text)
        return LoopState(
            messages=[summary_message],
            turn_count=state.turn_count,
            todos=state.todos,
        )
