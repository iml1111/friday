"""run_one_turn() — a single iteration of the agent loop.

Executes one turn: calls the provider, emits assistant messages, runs tool_use
blocks, and feeds tool_result back. On a context overflow the provider's
ContextOverflowError propagates to the caller (caller-owned compaction); on any
error path, unfinished tool_use blocks receive a synthetic error
tool_result so the tool_use<->tool_result pairing stays valid.

A turn ends by yielding exactly one sentinel: Terminal (loop done) or the next
LoopState (loop may continue). The caller drives the turn loop by calling
run_one_turn() in a while-true, advancing state on each LoopState until a Terminal
appears — there is no batch driver and no internal compaction.
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
    create_tool_result_message,
    create_user_message,
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
            # Only the text content is preserved; normalize_for_api applies the same rule.
            blocks.append(ContentBlock(type="thinking", text=block.thinking))

    # The response ID is not carried into the internal Message (no id field on Message).
    return Message(
        type="assistant",
        role="assistant",
        content=blocks,
    )


def _extract_tool_use_blocks(message: Message) -> list[ContentBlock]:
    """Return all tool_use ContentBlocks from an assistant Message."""
    return [block for block in message.content if block.type == "tool_use"]


def yield_missing_tool_result_blocks(
    tool_use_blocks: list[ContentBlock],
    emitted_results: list[Message],
    error_text: str,
) -> list[Message]:
    """Return synthetic error tool_result messages for any unfinished tool_use blocks.

    The LLM API requires that every tool_use block in the preceding assistant message
    has a matching tool_result in the next user message. When a turn is interrupted
    (on error) before all tools have run, this function backfills the missing
    entries so the pairing invariant is preserved and the next API call is not rejected.

    Args:
        tool_use_blocks: All tool_use blocks from the current assistant message.
        emitted_results: tool_result messages already yielded in this turn.
        error_text: Error string to embed in each synthetic tool_result.

    Returns:
        A list of synthetic error tool_result Messages for every unmatched tool_use.
    """
    emitted_ids = {
        block.tool_use_id
        for msg in emitted_results
        for block in msg.content
        if block.type == "tool_result" and block.tool_use_id
    }
    backfilled: list[Message] = []
    for block in tool_use_blocks:
        if block.id and block.id not in emitted_ids:
            backfilled.append(
                create_tool_result_message(
                    tool_use_id=block.id,
                    result_text=error_text,
                    is_error=True,
                )
            )
    return backfilled


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
    return (
        "<system-reminder>\n"
        "Current todo list (update via TodoWrite as you progress; keep one item in_progress):\n"
        f"{lines}\n"
        "This reflects tracked state, not necessarily the user's latest instruction.\n"
        "</system-reminder>"
    )


def with_todo_reminder(messages: list[Message], todos: list[dict]) -> list[Message]:
    """Return a turn-local message list with the todo reminder joined onto the
    trailing user turn. Never mutates the input messages, so state.messages and
    the persisted LoopState stay reminder-free (distributed-resume deterministic).
    """
    text = render_todo_reminder(todos)
    if not text:
        return messages
    block = ContentBlock(type="text", text=text)
    if messages and messages[-1].role == "user":
        last = messages[-1]
        merged = replace(last, content=[*last.content, block])   # new object; original untouched
        return [*messages[:-1], merged]
    return [*messages, create_user_message([block])]             # defensive: never hit at turn start


async def run_one_turn(
    *,
    provider: LLMProvider,
    tools: list[Tool],
    tool_schemas: list[dict],
    state: LoopState,
    system_prompt: str = "",
    config: LLMConfig | None = None,
    max_concurrency: int = 10,
) -> AsyncGenerator[Message | Terminal | LoopState, None]:
    """Execute a single turn of the agent loop.

    Yields all Messages produced in this turn, then yields exactly one sentinel:
      - Terminal: loop ends (completed / model_error).
      - LoopState: loop continues (next_turn) — the updated state for the next turn.

    Args:
        provider: LLM backend; only complete() is called.
        tools: Available tool instances.
        tool_schemas: Pre-built JSON schemas for each tool.
        state: Input loop state restored from the previous turn or initial state.
        system_prompt: Base system prompt text.
        config: LLM call configuration. Defaults to provider.config_type().
        max_concurrency: Maximum concurrent tool executions passed to run_tools.

    Yields:
        Message: messages produced this turn (assistant response, tool_result messages).
        Terminal | LoopState: exactly one sentinel as the final yield —
            Terminal when the loop ends, LoopState when it continues.

    Raises:
        ContextOverflowError: when the provider rejects the messages as too long.
            The caller should compact state via engine.compact() and retry.
    """
    config = config or provider.config_type()

    state_messages = state.messages
    turn_count = state.turn_count

    # API view only (turn-local, never persisted): inject the live todo reminder
    # so the model sees current progress without re-calling TodoWrite. The next
    # LoopState is assembled from the CLEAN state_messages below (no reminder leak).
    api_input_messages = list(state_messages)
    if state.todos:
        api_input_messages = with_todo_reminder(api_input_messages, state.todos)

    # Assemble the full system prompt for this turn.
    full_system_prompt = assemble_system_prompt(system_prompt)

    assistant_messages: list[Message] = []
    tool_results: list[Message] = []
    tool_use_blocks: list[ContentBlock] = []
    needs_follow_up = False

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
        for backfill_msg in yield_missing_tool_result_blocks(tool_use_blocks, tool_results, str(error)):
            yield backfill_msg
        yield Terminal(reason="model_error", error=error)
        return

    # Convert the response to an internal Message and yield it.
    message = _to_assistant_message(response)
    assistant_messages.append(message)
    yield message

    msg_tool_use_blocks = _extract_tool_use_blocks(message)
    if msg_tool_use_blocks:
        tool_use_blocks.extend(msg_tool_use_blocks)
        needs_follow_up = True

    # Termination point: no tool_use blocks — the model is done.
    if not needs_follow_up:
        yield Terminal(reason="completed")
        return

    # Execute all tool_use blocks, collecting results and declarative state effects.
    effects: list[dict] = []
    async for result_msg in run_tools(
        tool_use_blocks, tools, max_concurrency=max_concurrency, effects_sink=effects
    ):
        tool_results.append(result_msg)
        yield result_msg

    next_turn_count = turn_count + 1
    next_todos = apply_state_effects(state.todos, effects)

    # Continuation: assemble the next-turn LoopState from the CLEAN state_messages
    # (NOT api_input_messages) so the turn-local reminder is never persisted.
    yield LoopState(
        messages=[*state_messages, *assistant_messages, *tool_results],
        turn_count=next_turn_count,
        todos=next_todos,
    )
