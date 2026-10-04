"""Tool partitioning and parallel/sequential execution with block-order preservation.

Consecutive concurrency-safe tool calls are grouped into a single parallel batch; each
non-concurrency-safe call gets its own sequential batch. Within a parallel batch, results
are yielded in the original tool_use block order regardless of completion order (asyncio.gather
preserves argument order). Unknown tools and exceptions produce error tool_results instead of
aborting the batch.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import AsyncGenerator

from friday_agent.messages.types import ContentBlock, Message, create_tool_result_message
from friday_agent.tools.base import Tool, ToolResult


@dataclass
class Batch:
    """A group of tool calls to execute together.

    is_concurrency_safe=True means all blocks in this batch may run in parallel;
    is_concurrency_safe=False means the single block must run sequentially.
    """
    is_concurrency_safe: bool
    blocks: list[ContentBlock] = field(default_factory=list)


def _find_tool(tools: list[Tool], name: str) -> Tool | None:
    """Return the tool instance matching *name*, or None if not found."""
    for tool in tools:
        if tool.name == name:
            return tool
    return None


def _validated_input(tool: Tool, block: ContentBlock) -> dict | None:
    """Return the block input validated against the tool's schema, or None if it does not validate."""
    if block.input is None:
        return None
    try:
        return tool.input_schema().model_validate(block.input).model_dump()
    except Exception:
        return None


def _is_concurrency_safe(tool: Tool, block: ContentBlock) -> bool:
    """Return whether a single ContentBlock is concurrency-safe.

    Validates the block input against the tool's schema first; None or invalid
    input returns False (conservative fallback). On success, delegates to
    ``tool.is_concurrency_safe``; a raising predicate also counts as False.
    """
    parsed = _validated_input(tool, block)
    if parsed is None:
        return False
    try:
        return bool(tool.is_concurrency_safe(parsed))
    except Exception:
        return False


def is_deferred_call(block: ContentBlock, tools: list[Tool]) -> bool:
    """Return whether a tool_use block is held back for an external result.

    An unknown tool and input that fails schema validation count as not
    deferred: such a call runs inline — call() receives the raw input, and its
    own validation turns it into an immediate error for the model — instead of
    an external executor receiving a malformed call. A predicate that raises on
    valid input counts as deferred: a gate fails closed, so a well-formed call
    waits for its external result rather than running unchecked.
    """
    tool = _find_tool(tools, block.name or "")
    if tool is None:
        return False
    parsed = _validated_input(tool, block)
    if parsed is None:
        return False
    try:
        return bool(tool.is_deferred(parsed))
    except Exception:
        return True  # fail closed: hold the call rather than run it past a broken gate


def partition_tool_calls(
    blocks: list[ContentBlock],
    tools: list[Tool],
) -> list[Batch]:
    """Partition a list of tool_use blocks into ordered batches.

    Consecutive concurrency-safe blocks are merged into a single parallel batch
    (is_concurrency_safe=True). Each non-safe block becomes its own sequential
    batch (is_concurrency_safe=False).

    Example: [RO, RO, RO, MUT, RO, RO] → [Batch(True,3), Batch(False,1), Batch(True,2)]

    Args:
        blocks: tool_use ContentBlocks extracted from an LLM response.
        tools: available tool instances for the current turn.

    Returns:
        Ordered list of Batch objects ready for execution.
    """
    batches: list[Batch] = []

    for block in blocks:
        tool = _find_tool(tools, block.name or "")

        if tool is None:
            # Unknown tool — conservative non-safe fallback.
            is_safe = False
        else:
            is_safe = _is_concurrency_safe(tool, block)

        # Merge consecutive safe blocks into the current open parallel batch.
        if is_safe and batches and batches[-1].is_concurrency_safe:
            batches[-1].blocks.append(block)
        else:
            batches.append(Batch(is_concurrency_safe=is_safe, blocks=[block]))

    return batches


def to_tool_result_message(tool_use_id: str, result: ToolResult) -> Message:
    """Convert a ToolResult into its tool_result message (data, error flag, image).

    The single conversion shared by run_tools and core.loop.resume(), so results
    produced inside step() and results attached later behave identically.
    """
    return create_tool_result_message(
        tool_use_id=tool_use_id,
        result_text=str(result.data),
        is_error=result.is_error,
        image=result.image,
    )


# ---------------------------------------------------------------------------
# Single-tool execution helper
# ---------------------------------------------------------------------------

async def _run_single_tool(block: ContentBlock, tools: list[Tool]) -> tuple[Message, dict | None]:
    """Execute one tool call -> (tool_result message, state_effect | None).

    Unknown tool or any exception produces an error tool_result (and no effect)
    rather than crashing the batch.
    """
    tool = _find_tool(tools, block.name or "")

    if tool is None:
        return create_tool_result_message(
            tool_use_id=block.id or "",
            result_text=f"Error: Unknown tool: {block.name!r}",
            is_error=True,
        ), None

    try:
        result = await tool.call(block.input or {})
        return to_tool_result_message(block.id or "", result), result.state_effect
    except Exception as exc:
        return create_tool_result_message(
            tool_use_id=block.id or "",
            result_text=f"Error: {exc}",
            is_error=True,
        ), None


# ---------------------------------------------------------------------------
# run_tools — parallel/sequential execution with order preservation
# ---------------------------------------------------------------------------

async def run_tools(
    blocks: list[ContentBlock],
    tools: list[Tool],
    *,
    max_concurrency: int = 10,
    effects_sink: list[dict] | None = None,
) -> AsyncGenerator[Message, None]:
    """Execute tool calls partitioned into batches and yield tool_result messages.

    Parallel batches (is_concurrency_safe=True) run under asyncio.gather with a
    Semaphore cap; results are yielded in the original block order.
    Sequential batches run one block at a time.
    Unknown tools and exceptions produce error tool_results instead of crashing.

    If effects_sink is provided, each tool's non-None ToolResult.state_effect is
    appended to it in block order (the message stream is unchanged).

    Args:
        blocks: tool_use ContentBlocks to execute.
        tools: available tool instances.
        max_concurrency: maximum simultaneous tool calls (default 10).
        effects_sink: optional list that collects state_effects in block order.

    Yields:
        tool_result Messages in the same order as the input blocks.
    """
    semaphore = asyncio.Semaphore(max_concurrency)

    batches = partition_tool_calls(blocks, tools)

    for batch in batches:
        if batch.is_concurrency_safe and len(batch.blocks) > 1:
            # Parallel: Semaphore caps concurrency; gather preserves block order.
            async def _bounded(b: ContentBlock) -> tuple[Message, dict | None]:
                async with semaphore:
                    return await _run_single_tool(b, tools)

            results = await asyncio.gather(*[_bounded(b) for b in batch.blocks])
            for msg, effect in results:
                if effects_sink is not None and effect is not None:
                    effects_sink.append(effect)
                yield msg
        else:
            # Sequential: process each block one at a time.
            for block in batch.blocks:
                msg, effect = await _run_single_tool(block, tools)
                if effects_sink is not None and effect is not None:
                    effects_sink.append(effect)
                yield msg
