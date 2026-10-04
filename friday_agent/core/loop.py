"""run_one_turn() — a single iteration of the agent loop — and the pure state
functions around it, pending_tool_uses() and resume().

A turn calls the provider once, yields the assistant message and the
tool_results of the calls it ran, then one sentinel carrying the state to
persist. The caller drives the loop: there is no batch driver and no internal
compaction. The state functions need no provider and no tools, so any process
can attach late results.
"""
from __future__ import annotations

from dataclasses import replace
from typing import AsyncGenerator

from friday_agent.api.provider import (
    ContextOverflowError,
    LLMConfig,
    LLMError,
    LLMProvider,
    TextBlock,
    ThinkingBlock,
    ToolSchema,
    ToolUseBlock,
)
from friday_agent.core.state import LoopState, PendingToolUseError, Suspended, Terminal
from friday_agent.messages.normalize import normalize_for_api
from friday_agent.messages.types import ContentBlock, Message, create_user_message
from friday_agent.tools.base import Tool, ToolResult
from friday_agent.tools.orchestrator import is_deferred_call, run_tools, to_tool_result_message


def pending_tool_uses(state: LoopState) -> list[ContentBlock]:
    """Return the tool_use blocks still waiting for a result.

    Only the last assistant message can hold them (any earlier one was answered
    before the next model call), so pending calls are recomputed from history
    alone — LoopState needs no extra field, and a deserialized state gives the
    same answer. Pure function: no provider, no tools.
    """
    last = max((i for i, msg in enumerate(state.messages) if msg.role == "assistant"), default=None)
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


async def run_one_turn(
    *,
    provider: LLMProvider,
    tools: list[Tool],
    tool_schemas: list[ToolSchema],
    state: LoopState,
    system_prompt: str = "",
    config: LLMConfig | None = None,
    max_concurrency: int = 10,
    turn_reminders: list[str] | None = None,
) -> AsyncGenerator[Message | LoopState | Suspended | Terminal, None]:
    """Execute a single turn of the agent loop.

    Yields the assistant message, then the tool_result of each call it ran, then
    exactly one sentinel: LoopState (continue), Suspended (the response called
    deferred tools; the other calls ran, and Suspended.state waits for the
    deferred results — attach them with resume()), or Terminal (completed /
    model_error; Terminal.state is the state to persist).

    Args:
        provider: LLM backend; only complete() is called.
        tools: Available tool instances.
        tool_schemas: The tools' schemas as sent to the model.
        state: Input loop state restored from the previous turn or initial state.
        system_prompt: The full system prompt, sent verbatim (FridayAgent
            assembles it once with assemble_system_prompt()).
        config: LLM call configuration. Defaults to provider.config_type().
        max_concurrency: Maximum concurrent tool executions passed to run_tools.
        turn_reminders: Rendered turn-local reminders (FridayAgent passes the
            todo list, the memory index, then the caller's turn_sections),
            joined onto the trailing user message of the API view only — never
            persisted into LoopState.

    Raises:
        PendingToolUseError: state still has unanswered tool_use blocks
            (raised before the provider is called).
        ContextOverflowError: the provider rejected the messages as too long.
            The caller shrinks the state (trimming first, since engine.compact()
            re-sends the same prefix) and retries.
    """
    # Never send an unpaired tool_use: the API would reject it, and the 400
    # would surface only as an opaque model_error.
    if pending := pending_tool_uses(state):
        raise PendingToolUseError([block.id or "" for block in pending])

    # API view only: the reminders ride a copy of the trailing user message, so
    # state.messages (and every state built from it below) never carries them,
    # and everything before messages[-1] stays byte-stable for the prompt cache.
    api_messages = list(state.messages)
    if reminders := [ContentBlock(type="text", text=text) for text in turn_reminders or [] if text]:
        if api_messages and api_messages[-1].role == "user":
            api_messages[-1] = replace(api_messages[-1], content=[*api_messages[-1].content, *reminders])
        else:
            api_messages.append(create_user_message(reminders))

    try:
        response = await provider.complete(
            messages=normalize_for_api(api_messages),
            system_prompt=system_prompt,
            tools=tool_schemas,
            config=config or provider.config_type(),
        )
    except ContextOverflowError:
        # Caller-owned compaction: propagate so the caller can shrink and retry.
        raise
    except LLMError as error:
        # The call failed before any assistant message existed: nothing to pair,
        # nothing to add. The input state is the state to persist (and retry).
        yield Terminal(reason="model_error", error=error, state=state)
        return

    blocks: list[ContentBlock] = []
    for block in response.content:
        if isinstance(block, TextBlock):
            blocks.append(ContentBlock(type="text", text=block.text))
        elif isinstance(block, ToolUseBlock):
            blocks.append(ContentBlock(type="tool_use", id=block.id, name=block.name, input=block.input))
        elif isinstance(block, ThinkingBlock):
            # The signature is required when the block is echoed back on a later turn.
            blocks.append(ContentBlock(type="thinking", text=block.thinking, signature=block.signature))
    message = Message(type="assistant", role="assistant", content=blocks)
    yield message

    # Deferred calls wait for an external result; every other call runs now.
    deferred: list[ContentBlock] = []
    immediate: list[ContentBlock] = []
    for block in blocks:
        if block.type == "tool_use":
            (deferred if is_deferred_call(block, tools) else immediate).append(block)

    effects: list[dict] = []
    tool_results: list[Message] = []
    async for result_msg in run_tools(immediate, tools, max_concurrency=max_concurrency, effects_sink=effects):
        tool_results.append(result_msg)
        yield result_msg

    next_state = LoopState(
        messages=[*state.messages, message, *tool_results],
        turn_count=state.turn_count + 1,
        todos=_apply_state_effects(state.todos, effects),
    )
    if deferred:
        yield Suspended(state=next_state, pending=deferred)
    elif immediate:
        yield next_state
    else:
        # No tool_use blocks: the model is done.
        yield Terminal(reason="completed", state=next_state)


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

    # Results are non-empty and all pending, so the last assistant message exists.
    last = max(i for i, msg in enumerate(state.messages) if msg.role == "assistant")
    order = {block.id: n for n, block in enumerate(state.messages[last].content) if block.type == "tool_use"}
    ids = sorted(results, key=order.__getitem__)
    answers: list[Message] = []
    others: list[Message] = []
    for msg in state.messages[last + 1:]:
        # A tool_result-only message answering this assistant message's calls.
        is_answer = bool(msg.content) and all(
            block.type == "tool_result" and block.tool_use_id in order for block in msg.content
        )
        (answers if is_answer else others).append(msg)
    answers += [to_tool_result_message(tool_use_id, results[tool_use_id]) for tool_use_id in ids]
    answers.sort(key=lambda msg: order[msg.content[0].tool_use_id])
    effects = [results[i].state_effect for i in ids if results[i].state_effect is not None]
    return LoopState(
        messages=[*state.messages[: last + 1], *answers, *others],
        turn_count=state.turn_count,
        todos=_apply_state_effects(state.todos, effects),
    )


def _apply_state_effects(todos: list[dict], effects: list[dict]) -> list[dict]:
    """Fold tools' declarative state_effects into the todos (last write wins).

    Tools never write state: they return effects, and the loop applies them
    here. Only the "todos" key is known; any other key is ignored.
    """
    for effect in effects:
        if "todos" in effect:
            todos = effect["todos"]
    return todos
