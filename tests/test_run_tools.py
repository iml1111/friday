"""run_tools parallel/sequential execution tests.

Result order must match block order even when tools finish out of order
(asyncio.gather preserves argument order).
"""
import asyncio

import pytest
from pydantic import BaseModel

from friday_agent.messages.types import ContentBlock
from friday_agent.tools.base import Tool, ToolResult
from friday_agent.tools.orchestrator import run_tools


class _SlowInput(BaseModel):
    d: float = 0.0
    i: int = 0


class _Slow(Tool):
    name = "Slow"

    def input_schema(self):
        return _SlowInput

    def is_concurrency_safe(self, inp):
        return True

    async def call(self, args):
        await asyncio.sleep(args.get("d", 0))
        return ToolResult(data=args.get("i"))


def _b(i, d):
    return ContentBlock(type="tool_use", id=f"t{i}", name="Slow", input={"d": d, "i": i})


@pytest.mark.asyncio
async def test_results_in_block_order_despite_parallel():
    # Slowest tool is first; results must still arrive in block order.
    blocks = [_b(0, 0.05), _b(1, 0.0), _b(2, 0.02)]
    out = []
    async for msg in run_tools(blocks, [_Slow()]):
        out.append(msg.content[0].tool_use_id)
    assert out == ["t0", "t1", "t2"]
