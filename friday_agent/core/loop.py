"""run_one_turn() — a single iteration of the agent loop.

Executes one turn: calls the provider, emits the assistant message, runs its
tool_use blocks, and feeds the tool_results back. On a context overflow the
provider's ContextOverflowError propagates to the caller (caller-owned
compaction). The only other error path — an LLMError from the provider call —
fires before any assistant message exists, so no tool_use is ever left unpaired.

A turn ends by yielding exactly one sentinel, each carrying the state to
persist: the next LoopState (continue), Suspended (the response called
deferred tools; the other calls ran, and the deferred results arrive later via
resume()), or Terminal (done). The caller drives the turn loop by calling
run_one_turn() in a while-true — there is no batch driver and no internal
compaction. pending_tool_uses() and resume() are pure state functions (no
provider, no tools), so any process can attach late results.
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
from friday_agent.core.state import LoopState, PendingToolUseError, Suspended, Terminal
from friday_agent.messages.normalize import normalize_for_api
from friday_agent.messages.types import (
    ContentBlock,
    Message,
    create_user_message,
    wrap_system_reminder,
)
from friday_agent.tools.base import Tool, ToolResult
from friday_agent.tools.orchestrator import is_deferred_call, run_tools, to_tool_result_message


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


def _last_assistant_index(messages: list[Message]) -> int | None:
    """Index of the last assistant message, or None if there is none."""
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].role == "assistant":
            return i
    return None


def pending_tool_uses(state: LoopState) -> list[ContentBlock]:
    """Return the tool_use blocks still waiting for a result.

    Only the last assistant message can hold them (any earlier one was answered
    before the next model call), so pending calls are recomputed from history
    alone — LoopState needs no extra field, and a deserialized state gives the
    same answer. Pure function: no provider, no tools.
    """
    last = _last_assistant_index(state.messages)
    if last is None:
        return []
    answered = {
        block.tool_use_id
        for msg in state.messages[last + 1:]
        for block in msg.content
        if block.type == "tool_result"
    }
    return [
        block
        for block in state.messages[last].content
        if block.type == "tool_use" and block.id not in answered
    ]


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
) -> AsyncGenerator[Message | LoopState | Suspended | Terminal, None]:
    """Execute a single turn of the agent loop.

    Yields all Messages produced in this turn, then yields exactly one sentinel:
      - LoopState: loop continues — the updated state for the next turn.
      - Suspended: the response called deferred tools; the other calls ran, and
        Suspended.state waits for the deferred results (attach with resume()).
      - Terminal: loop ends (completed / model_error); Terminal.state is the
        state to persist.

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
        LoopState | Suspended | Terminal: exactly one sentinel as the final yield —
            LoopState when it continues, Suspended when it waits on deferred
            calls, Terminal when the loop ends.

    Raises:
        PendingToolUseError: when state still has unanswered tool_use blocks
            (raised before the provider is called).
        ContextOverflowError: when the provider rejects the messages as too long.
            The caller shrinks the state (trimming first, since engine.compact()
            re-sends the same prefix) and retries.
    """
    # Never send an unpaired tool_use: the API would reject it, and the 400
    # would surface only as an opaque model_error.
    if pending := pending_tool_uses(state):
        raise PendingToolUseError([block.id or "" for block in pending])

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

    # Deferred calls wait for an external result; every other call runs now.
    held = [is_deferred_call(block, tools) for block in tool_use_blocks]
    deferred = [block for block, h in zip(tool_use_blocks, held) if h]
    immediate = [block for block, h in zip(tool_use_blocks, held) if not h]

    effects: list[dict] = []
    tool_results: list[Message] = []
    async for result_msg in run_tools(
        immediate, tools, max_concurrency=max_concurrency, effects_sink=effects
    ):
        tool_results.append(result_msg)
        yield result_msg

    # Assemble the next state from the CLEAN state.messages (NOT
    # api_input_messages) so the turn-local reminders are never persisted.
    next_state = LoopState(
        messages=[*state.messages, message, *tool_results],
        turn_count=state.turn_count + 1,
        todos=apply_state_effects(state.todos, effects),
    )
    yield Suspended(state=next_state, pending=deferred) if deferred else next_state


def _answers(msg: Message, order: dict[str, int]) -> bool:
    """Whether msg is a tool_result-only message answering a call in `order`."""
    return bool(msg.content) and all(
        block.type == "tool_result" and block.tool_use_id in order for block in msg.content
    )


def resume(state: LoopState, results: dict[str, ToolResult]) -> LoopState:
    """Attach externally produced results to a suspended state. Never calls the model.

    Each result becomes a tool_result message through the same conversion
    run_tools uses (to_tool_result_message — errors and images behave
    identically), and state_effects are applied in tool_use order. All results
    for the last assistant message end up in tool_use order, ahead of any other
    trailing message (tool results must precede text in a user turn — this also
    repairs a state that got a user message appended before resume). Partial
    results are allowed: the rest stay pending, and step() keeps refusing the
    state until they are attached. Returns a new state; the input is untouched,
    and turn_count is unchanged (the turn was counted when it suspended).

    Raises:
        ValueError: a key is not a pending tool_use id (unknown or already answered).
    """
    pending_ids = {block.id for block in pending_tool_uses(state)}
    invalid = [tool_use_id for tool_use_id in results if tool_use_id not in pending_ids]
    if invalid:
        raise ValueError(f"resume: not pending (unknown or already answered): {invalid}")
    if not results:
        return LoopState(messages=list(state.messages), turn_count=state.turn_count, todos=state.todos)

    last = _last_assistant_index(state.messages)  # set: results are non-empty and all pending
    order = {block.id: n for n, block in enumerate(_extract_tool_use_blocks(state.messages[last]))}
    ids = sorted(results, key=order.__getitem__)
    trailing = state.messages[last + 1:]
    answers = [msg for msg in trailing if _answers(msg, order)]
    answers += [to_tool_result_message(tool_use_id, results[tool_use_id]) for tool_use_id in ids]
    answers.sort(key=lambda msg: order[msg.content[0].tool_use_id])
    others = [msg for msg in trailing if not _answers(msg, order)]
    effects = [results[i].state_effect for i in ids if results[i].state_effect is not None]
    return LoopState(
        messages=[*state.messages[: last + 1], *answers, *others],
        turn_count=state.turn_count,
        todos=apply_state_effects(state.todos, effects),
    )
