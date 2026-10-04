"""run_one_turn() — a single iteration of the agent loop.

Executes one turn: calls the provider, emits the assistant message, runs its
tool_use blocks, and feeds the tool_results back. On a context overflow the
provider's ContextOverflowError propagates to the caller (caller-owned
compaction). The only other error path — an LLMError from the provider call —
fires before any assistant message exists, so no tool_use is ever left unpaired.

A turn ends by yielding exactly one sentinel: Terminal (loop done; carries the
state to persist) or the next LoopState (loop may continue). The caller drives
the turn loop by calling run_one_turn() in a while-true, advancing state on each
LoopState until a Terminal appears — there is no batch driver and no internal
compaction.
"""
from __future__ import annotations

from dataclasses import replace
from typing import AsyncGenerator

from friday_agent.api.provider import (
    AssistantResponse,
    ContextOverflowError,
    LLMConfig,
    LLMError,
    LLMProvider,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)
from friday_agent.api.prompts import assemble_system_prompt
from friday_agent.core.state import LoopState, Terminal
from friday_agent.messages.normalize import normalize_for_api
from friday_agent.messages.types import (
    ContentBlock,
    Message,
    create_user_message,
    wrap_system_reminder,
)
from friday_agent.tools.base import Tool
from friday_agent.tools.orchestrator import run_tools


def _to_assistant_message(response: AssistantResponse) -> Message:
    """Convert an AssistantResponse to an internal Message with a flat ContentBlock list."""
    blocks: list[ContentBlock] = []
    for block in response.content:
        if isinstance(block, TextBlock):
            blocks.append(ContentBlock(type="text", text=block.text))
        elif isinstance(block, ToolUseBlock):
            blocks.append(
                ContentBlock(
                    type="tool_use",
                    id=block.id,
                    name=block.name,
                    input=block.input,
                )
            )
        elif isinstance(block, ThinkingBlock):
            # Preserve both the thinking text and its signature; the API requires the
            # signature when the block is echoed back on a later turn.
            blocks.append(ContentBlock(type="thinking", text=block.thinking, signature=block.signature))

    # The response ID is not carried into the internal Message (no id field on Message).
    return Message(
        type="assistant",
        role="assistant",
        content=blocks,
    )


def _extract_tool_use_blocks(message: Message) -> list[ContentBlock]:
    """Return all tool_use ContentBlocks from an assistant Message."""
    return [block for block in message.content if block.type == "tool_use"]


def apply_state_effects(todos: list[dict], effects: list[dict]) -> list[dict]:
    """Fold declarative tool state_effects into the todos list (last write wins).

    The loop is the sole state writer; tools only return effects. This applier
    knows the 'todos' state concept — not any specific tool name — so callers can
    add their own stateful tools without touching the loop.
    """
    for eff in effects:
        if "todos" in eff:
            todos = eff["todos"]
    return todos


def render_todo_reminder(todos: list[dict]) -> str:
    """Render the live todo list as a <system-reminder> block. Empty list -> ''."""
    if not todos:
        return ""
    lines = "\n".join(
        f"- [{t.get('status', 'pending')}] {t.get('content', '')}" for t in todos
    )
    return wrap_system_reminder(
        "Current todo list (update via TodoWrite as you progress; keep one item in_progress):\n"
        f"{lines}\n"
        "This reflects tracked state, not necessarily the user's latest instruction."
    )


def with_turn_reminders(messages: list[Message], texts: list[str]) -> list[Message]:
    """Return a turn-local message list with reminder text blocks joined onto
    the trailing user turn, in the given order (empty strings dropped). Never
    mutates the input messages, so state.messages and the persisted LoopState
    stay reminder-free (distributed-resume deterministic).

    Cache invariant: all per-turn mutable text rides messages[-1] only, keeping
    messages[-2] and earlier byte-stable so the provider's rolling cache
    breakpoints keep hitting (see anthropic_provider._apply_cache_control).
    """
    blocks = [ContentBlock(type="text", text=t) for t in texts if t]
    if not blocks:
        return messages
    if messages and messages[-1].role == "user":
        last = messages[-1]
        merged = replace(last, content=[*last.content, *blocks])  # new object; original untouched
        return [*messages[:-1], merged]
    return [*messages, create_user_message(blocks)]              # defensive: never hit at turn start


def with_todo_reminder(messages: list[Message], todos: list[dict]) -> list[Message]:
    """Turn-local todo reminder on the trailing user turn (with_turn_reminders shorthand)."""
    return with_turn_reminders(messages, [render_todo_reminder(todos)])


async def run_one_turn(
    *,
    provider: LLMProvider,
    tools: list[Tool],
    tool_schemas: list[dict],
    state: LoopState,
    system_prompt: str = "",
    config: LLMConfig | None = None,
    max_concurrency: int = 10,
    turn_reminders: list[str] | None = None,
) -> AsyncGenerator[Message | Terminal | LoopState, None]:
    """Execute a single turn of the agent loop.

    Yields all Messages produced in this turn, then yields exactly one sentinel:
      - Terminal: loop ends (completed / model_error); Terminal.state is the
        state to persist.
      - LoopState: loop continues (next_turn) — the updated state for the next turn.

    Args:
        provider: LLM backend; only complete() is called.
        tools: Available tool instances.
        tool_schemas: Pre-built JSON schemas for each tool.
        state: Input loop state restored from the previous turn or initial state.
        system_prompt: Base system prompt text.
        config: LLM call configuration. Defaults to provider.config_type().
        max_concurrency: Maximum concurrent tool executions passed to run_tools.
        turn_reminders: Pre-rendered turn-local reminder texts (e.g. a live
            memory index) appended after the todo reminder onto the trailing
            user message of the API view only — never persisted into LoopState.

    Yields:
        Message: messages produced this turn (assistant response, tool_result messages).
        Terminal | LoopState: exactly one sentinel as the final yield —
            Terminal when the loop ends, LoopState when it continues.

    Raises:
        ContextOverflowError: when the provider rejects the messages as too long.
            The caller should compact state via engine.compact() and retry.
    """
    config = config or provider.config_type()

    # API view only (turn-local, never persisted): inject the live todo reminder
    # and any caller-supplied turn reminders (e.g. memory index) so the model
    # sees current state without it entering the durable history. The next
    # state is assembled from the CLEAN state.messages below (no reminder leak).
    reminder_texts = [render_todo_reminder(state.todos)] if state.todos else []
    reminder_texts.extend(turn_reminders or [])
    api_input_messages = with_turn_reminders(list(state.messages), reminder_texts)

    # Assemble the full system prompt for this turn.
    full_system_prompt = assemble_system_prompt(system_prompt)

    # Call the LLM.
    api_messages = normalize_for_api(api_input_messages)
    try:
        response = await provider.complete(
            messages=api_messages,
            system_prompt=str(full_system_prompt),
            tools=tool_schemas,
            config=config,
        )
    except ContextOverflowError:
        # Caller-owned compaction: propagate so the caller can compact and retry.
        raise
    except LLMError as error:
        # The call failed before any assistant message existed: nothing to pair,
        # nothing to add. The input state is the state to persist (and retry).
        yield Terminal(reason="model_error", error=error, state=state)
        return

    # Convert the response to an internal Message and yield it.
    message = _to_assistant_message(response)
    yield message

    tool_use_blocks = _extract_tool_use_blocks(message)

    # Termination point: no tool_use blocks — the model is done.
    if not tool_use_blocks:
        yield Terminal(
            reason="completed",
            state=LoopState(
                messages=[*state.messages, message],
                turn_count=state.turn_count + 1,
                todos=state.todos,
            ),
        )
        return

    # Execute all tool_use blocks, collecting results and declarative state effects.
    effects: list[dict] = []
    tool_results: list[Message] = []
    async for result_msg in run_tools(
        tool_use_blocks, tools, max_concurrency=max_concurrency, effects_sink=effects
    ):
        tool_results.append(result_msg)
        yield result_msg

    # Continuation: assemble the next-turn LoopState from the CLEAN state.messages
    # (NOT api_input_messages) so the turn-local reminder is never persisted.
    yield LoopState(
        messages=[*state.messages, message, *tool_results],
        turn_count=state.turn_count + 1,
        todos=apply_state_effects(state.todos, effects),
    )
