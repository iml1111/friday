"""Engine integration: always-on memory, replaceable store, collision, compact-clean."""
import pytest
from pydantic import BaseModel

from friday_agent.api.provider import (
    AssistantResponse,
    StopReason,
    TextBlock,
    TokenUsage,
    ToolUseBlock,
)
from friday_agent.core.engine import FridayAgent
from friday_agent.core.state import LoopState
from friday_agent.memory.store import MEMORY_INSTRUCTIONS, MemoryEntry, MemoryType
from friday_agent.messages.types import create_user_message
from friday_agent.tools.base import Tool, ToolResult
from tests._drive import collect_turn
from tests.fakes import FakeLLMProvider, InMemoryStore


def _end():
    return AssistantResponse(content=[TextBlock(text="ok")], stop_reason=StopReason.END_TURN, usage=TokenUsage())


@pytest.mark.asyncio
async def test_memory_tools_registered_alongside_caller_tools():
    fake = FakeLLMProvider(responses=[_end()])
    engine = FridayAgent(provider=fake, tools=[], memory=InMemoryStore())
    await collect_turn(engine, LoopState(messages=[create_user_message("hi")]))
    names = {t["name"] for t in fake.received_tools[0]}
    assert {"memory_save", "memory_read", "memory_delete"} <= names


@pytest.mark.asyncio
async def test_memory_instructions_injected_into_system_prompt():
    fake = FakeLLMProvider(responses=[_end()])
    engine = FridayAgent(provider=fake, tools=[], system_prompt="BASE", memory=InMemoryStore())
    await collect_turn(engine, LoopState(messages=[create_user_message("hi")]))
    sp = fake.received_system_prompts[0]
    assert "BASE" in sp
    assert MEMORY_INSTRUCTIONS in sp
    # Generic -> specific: SDK memory instructions precede the domain prompt.
    assert sp.index(MEMORY_INSTRUCTIONS) < sp.index("BASE")


@pytest.mark.asyncio
async def test_memory_index_rides_turn_reminder_not_system_prompt():
    # The live index is per-turn mutable: it must ride messages[-1] as a
    # <system-reminder> (cache-safe), never the system prefix.
    store = InMemoryStore()
    await store.save(MemoryEntry(name="user-role", description="backend eng", type=MemoryType.user, body="B"))
    fake = FakeLLMProvider(responses=[_end()])
    engine = FridayAgent(provider=fake, tools=[], system_prompt="BASE", memory=store)
    await collect_turn(engine, LoopState(messages=[create_user_message("hi")]))

    assert "[user] user-role" not in fake.received_system_prompts[0]
    last = fake.received_messages[0][-1]
    joined = "".join(b.get("text", "") for b in last["content"])
    assert "<system-reminder>" in joined
    assert "[user] user-role — backend eng" in joined


@pytest.mark.asyncio
async def test_empty_store_adds_no_reminder_block():
    fake = FakeLLMProvider(responses=[_end()])
    engine = FridayAgent(provider=fake, tools=[], memory=InMemoryStore())
    await collect_turn(engine, LoopState(messages=[create_user_message("hi")]))
    last = fake.received_messages[0][-1]
    assert [b.get("text") for b in last["content"]] == ["hi"]  # no extra block


def test_caller_tool_colliding_with_memory_name_raises():
    class Clash(Tool):
        name = "memory_save"
        def input_schema(self):
            class I(BaseModel):
                pass
            return I
        async def call(self, args):
            return ToolResult(data="x")

    with pytest.raises(ValueError, match="memory_save"):
        FridayAgent(provider=FakeLLMProvider(responses=[]), tools=[Clash()], memory=InMemoryStore())


@pytest.mark.asyncio
async def test_injected_store_tools_replace_defaults():
    class Recall(Tool):
        name = "my_recall"
        def input_schema(self):
            class I(BaseModel):
                pass
            return I
        async def call(self, args):
            return ToolResult(data="x")

    class CustomStore(InMemoryStore):
        def tools(self):
            return [Recall()]

    fake = FakeLLMProvider(responses=[_end()])
    engine = FridayAgent(provider=fake, tools=[], memory=CustomStore())
    await collect_turn(engine, LoopState(messages=[create_user_message("hi")]))
    names = {t["name"] for t in fake.received_tools[0]}
    assert "my_recall" in names
    assert "memory_save" not in names   # default surface fully replaced


@pytest.mark.asyncio
async def test_compact_does_not_inject_memory_section():
    summary = AssistantResponse(
        content=[TextBlock(text="<summary>S</summary>")],
        stop_reason=StopReason.END_TURN, usage=TokenUsage(),
    )
    fake = FakeLLMProvider(responses=[summary])
    engine = FridayAgent(provider=fake, tools=[], system_prompt="BASE", memory=InMemoryStore())
    await engine.compact(LoopState(messages=[create_user_message("a")], turn_count=1))
    assert MEMORY_INSTRUCTIONS not in fake.received_system_prompts[0]


@pytest.mark.asyncio
async def test_memory_save_tool_persists_to_injected_store():
    save = AssistantResponse(
        content=[ToolUseBlock(
            id="m1", name="memory_save",
            input={"name": "user-role", "description": "backend eng", "type": "user", "body": "Go 10y"},
        )],
        stop_reason=StopReason.TOOL_USE, usage=TokenUsage(),
    )
    store = InMemoryStore()
    engine = FridayAgent(provider=FakeLLMProvider(responses=[save, _end()]), tools=[], memory=store)
    await collect_turn(engine, LoopState(messages=[create_user_message("remember my role")]))
    got = await store.read("user-role")
    assert got is not None and got.body == "Go 10y"


@pytest.mark.asyncio
async def test_memory_body_does_not_leak_into_loopstate_serde():
    save = AssistantResponse(
        content=[ToolUseBlock(
            id="m1", name="memory_save",
            input={"name": "secret", "description": "d", "type": "user", "body": "SENSITIVE-BODY"},
        )],
        stop_reason=StopReason.TOOL_USE, usage=TokenUsage(),
    )
    engine = FridayAgent(provider=FakeLLMProvider(responses=[save, _end()]), tools=[], memory=InMemoryStore())
    _, outcome = await collect_turn(engine, LoopState(messages=[create_user_message("save it")]))
    assert isinstance(outcome, LoopState)
    assert set(outcome.to_dict().keys()) == {"messages", "turn_count", "todos"}


def test_default_memory_is_file_store_and_registers_tools():
    # memory=None → default FileMemoryStore; construction triggers no file I/O.
    from friday_agent.memory.store import FileMemoryStore

    engine = FridayAgent(provider=FakeLLMProvider(responses=[]), tools=[])
    names = {t.name for t in engine._tools}
    assert {"memory_save", "memory_read", "memory_delete"} <= names
    assert isinstance(engine._memory, FileMemoryStore)
