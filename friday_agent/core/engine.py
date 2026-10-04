"""FridayAgent — external entry point that runs one turn of the agent loop.

Holds the provider, tools, and call configuration, and exposes step(state): an
async generator that yields each Message produced during the turn (assistant
response, then each tool_result) and finally yields exactly one sentinel —
the next LoopState (continue), Suspended (paused on deferred tool calls) or
Terminal (ended). The caller drives the turn loop by calling step() until the
sentinel is a Terminal.
"""
from __future__ import annotations

import copy
from collections import Counter
from typing import AsyncGenerator

from friday_agent.api.prompts import assemble_system_prompt, format_compact_prompt, format_compact_summary_message
from friday_agent.api.provider import AssistantResponse, LLMConfig, LLMError, LLMProvider, ToolSchema, ToolUseBlock
from friday_agent.memory.store import (
    MEMORY_INSTRUCTIONS,
    MemoryStore,
    build_memory_reminder,
)
from friday_agent.core.loop import pending_tool_uses, run_one_turn
from friday_agent.core.state import LoopState, PendingToolUseError, Suspended, Terminal
from friday_agent.messages.normalize import normalize_for_api
from friday_agent.messages.types import Message, create_user_message, wrap_system_reminder
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
        system_prompt: Domain system prompt. assemble_system_prompt() puts the
                SDK guidance (and MEMORY_INSTRUCTIONS when memory is mounted)
                ahead of it.
        config: Vendor call configuration, passed directly (e.g. AnthropicConfig).
                Defaults to the provider's default config (provider.config_type())
                when None.
        max_concurrency: Maximum concurrent tool executions (default 10).
        memory: Optional MemoryStore. When None (the default), the memory
                subsystem is not mounted: no memory tools, no MEMORY_INSTRUCTIONS
                system section, no per-turn index reminder. Pass a store
                (e.g. FileMemoryStore()) to opt in.
        compact_instructions: Domain requirements folded into the compaction
                prompt used by compact() — the channel for what a summary must
                preserve. (system_prompt also reaches that call, but only as
                the shared cached prefix, ahead of the compact prompt.)
                Empty (the default) leaves the base prompt untouched.

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
        # Assembled once: it heads the cached prefix, and step() and compact()
        # both send it, so it must not change within a session.
        self._system_prompt = assemble_system_prompt(
            system_prompt, MEMORY_INSTRUCTIONS if memory is not None else ""
        )
        self._config = config
        self._max_concurrency = max_concurrency
        self._compact_instructions = compact_instructions

    def _tool_schemas(self) -> list[ToolSchema]:
        return [tool.get_tool_schema() for tool in self._tools]

    async def step(
        self, state: LoopState, turn_sections: list[str] | None = None
    ) -> AsyncGenerator[Message | LoopState | Suspended | Terminal, None]:
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

        Args:
            state: The turn's input state.
            turn_sections: Per-turn context for this call only (current screen,
                progress). Each non-empty string is wrapped in a <system-reminder>
                and joined onto the trailing user message of the API view (after
                the todo reminder and the memory index) — never persisted into
                LoopState, never part of the cached prefix. Pass different
                sections (or none) on each call; static content belongs in
                system_prompt.

        Raises:
            PendingToolUseError: the state still has unanswered tool_use blocks —
                raised by run_one_turn before any request.
            ContextOverflowError: propagated from run_one_turn during iteration when the
                provider rejects the messages as too long. The caller shrinks the
                state and retries — compact() re-sends this same prefix, so trim
                the oldest turns first (see compact()).
        """
        # Per-turn content (the live memory index, then the caller's
        # turn_sections) rides messages[-1] as turn-local reminders — in the
        # system prompt it would invalidate the whole conversation cache.
        turn_reminders = [
            reminder
            for reminder in (
                await build_memory_reminder(self._memory) if self._memory is not None else "",
                *(wrap_system_reminder(text) for text in turn_sections or [] if text),
            )
            if reminder
        ]
        async for item in run_one_turn(
            provider=self._provider,
            tools=self._tools,
            tool_schemas=self._tool_schemas(),
            state=state,
            system_prompt=self._system_prompt,
            config=self._config,
            max_concurrency=self._max_concurrency,
            turn_reminders=turn_reminders,
        ):
            yield item

    async def compact(self, state: LoopState) -> LoopState:
        """Summarize the entire conversation into one summary message and return a smaller LoopState.

        Replaces all of state.messages with a single summary message; turn_count
        and todos are preserved.

        The summary call sends exactly the system prompt, tool schemas and config
        step() sends (max_tokens raised to at least 20,000), so the provider
        serves the conversation from its prompt cache instead of writing it again.
        A reply that calls a tool or lacks a usable <summary> is retried once with
        tools=[]. compact_instructions (constructor) is the injection point for
        domain requirements about what the summary must preserve. turn_sections
        (a step() argument) never reach the summary call.

        Args:
            state: The state to compact.

        Raises:
            PendingToolUseError: the state still has unanswered tool_use blocks
                (its summary call would send them unpaired).
            ContextOverflowError: the summary call is step()'s request plus the
                compact prompt, so a state that already overflowed step()
                overflows here too. Shrinking it first (e.g. dropping the oldest
                turns, keeping tool_use/tool_result pairs) is the caller's job.
            LLMError: the summary call failed, or its reply has no text even
                after the retry — the caller keeps its state.
        """
        if pending := pending_tool_uses(state):
            raise PendingToolUseError([block.id or "" for block in pending])
        messages = [
            *normalize_for_api(state.messages),
            {"role": "user", "content": format_compact_prompt(self._compact_instructions)},
        ]
        # Same config as step() (the message cache keys on settings such as
        # thinking); only the output budget is raised for the summary.
        config = copy.copy(self._config)
        config.max_tokens = max(self._config.max_tokens, 20_000)

        response = await self._provider.complete(
            messages=messages, system_prompt=self._system_prompt, tools=self._tool_schemas(), config=config,
        )
        if self._has_tool_use(response) or self._extract_summary(self._response_text(response)) is None:
            # The shared prefix exposes the agent's tools: a tool call (or a reply
            # with no summary) is retried once without them — the only call that
            # pays the full price for the history. A tool_use never enters state.
            response = await self._provider.complete(
                messages=messages, system_prompt=self._system_prompt, tools=[], config=config,
            )

        raw_text = self._response_text(response)
        if not raw_text.strip():
            # Nothing to fall back on: an empty summary would wipe the history.
            raise LLMError("compact: the summary reply has no text")
        # No usable <summary> pair — keep the full response as a best-effort fallback.
        summary_text = self._extract_summary(raw_text) or raw_text.strip()
        summary_message = create_user_message(
            content=format_compact_summary_message(summary_text), is_compact_summary=True,
        )
        return LoopState(messages=[summary_message], turn_count=state.turn_count, todos=state.todos)

    @staticmethod
    def _response_text(response: AssistantResponse) -> str:
        """Concatenate the response's text blocks."""
        return "".join(block.text for block in response.content if getattr(block, "text", None))

    @staticmethod
    def _has_tool_use(response: AssistantResponse) -> bool:
        return any(isinstance(block, ToolUseBlock) for block in response.content)

    @staticmethod
    def _extract_summary(raw_text: str) -> str | None:
        """Return the text inside ``<summary>``, or None if there is no usable pair.

        The closing tag is searched for only *after* the opening tag: a summarizer
        that closes its ``<analysis>`` block with ``</summary>`` by mistake puts a
        closing tag ahead of the real opening one, and pairing the first of each
        yields an empty summary. An empty body is reported as a miss as well, so
        compact() falls back instead of replacing the history with nothing.
        """
        start = raw_text.find("<summary>")
        if start == -1:
            return None
        start += len("<summary>")
        end = raw_text.find("</summary>", start)
        if end == -1:
            return None
        return raw_text[start:end].strip() or None
