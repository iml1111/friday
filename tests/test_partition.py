from friday_agent.tools.orchestrator import partition_tool_calls
from friday_agent.messages.types import ContentBlock
from friday_agent.tools.builtin.example_tool import ExampleTool


def _b(i, mutating):
    return ContentBlock(type="tool_use", id=f"t{i}", name="ExampleTool",
                        input={"payload": "p", "mutating": mutating})


def test_partition_groups_consecutive_readonly():
    # [RO,RO,RO,MUT,RO,RO] → [parallel(3), serial(1), parallel(2)]
    blocks = [_b(0,False),_b(1,False),_b(2,False),_b(3,True),_b(4,False),_b(5,False)]
    batches = partition_tool_calls(blocks, [ExampleTool()])
    shapes = [(b.is_concurrency_safe, len(b.blocks)) for b in batches]
    assert shapes == [(True,3),(False,1),(True,2)]


def test_unparseable_input_is_conservative():
    bad = ContentBlock(type="tool_use", id="x", name="ExampleTool", input=None)
    batches = partition_tool_calls([bad], [ExampleTool()])
    assert batches[0].is_concurrency_safe is False   # unparseable input → conservative (not safe)
