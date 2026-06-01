# Todo / Task Tracking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Claude-Code-style live todo reminder to `friday_agent` — a `TodoWrite` tool whose list is re-injected into every turn's API view so the model never loses track of multi-step work, while preserving friday's pure-tool / single-state-writer / distributed-resume invariants.

**Architecture:** Todos live as a JSON-native `LoopState.todos` field (so they ride distributed-resume serialization). Pure tools never mutate state — `TodoWrite` returns a declarative `ToolResult.state_effect={"todos": [...]}`, and the loop (the sole state writer) folds effects into the next `LoopState` via `apply_state_effects`. Each turn, the loop renders the *committed* todos into a `<system-reminder>` text block joined onto the trailing user turn of a **turn-local API-view copy** (`api_input_messages`) — never the persisted state, so resume regenerates it deterministically.

**Tech Stack:** Python 3.11+, `pydantic` (tool input schema), `anyio`/`asyncio` (orchestrator), `pytest` + `pytest-asyncio` (keyless tests via `FakeLLMProvider`).

**Authoritative spec:** `docs/proposals/2026-06-01-todo-tracking-design.md` (reconciled to the current `LoopState`-as-sentinel codebase).

---

## File Structure

**Modified:**
- `friday_agent/core/state.py` — add `LoopState.todos: list[dict]` field + serde.
- `friday_agent/tools/base.py` — add `ToolResult.state_effect: dict | None`.
- `friday_agent/core/loop.py` — add three todos-state helpers (`apply_state_effects`, `render_todo_reminder`, `with_todo_reminder`) and wire them into `run_one_turn`. **Core owns the todos-state read/apply** (it already keys `apply_state_effects` on the `'todos'` concept, not on any tool name), so the renderer lives here, not in the tool — the loop imports nothing from `tools/builtin`.
- `friday_agent/tools/orchestrator.py` — `_run_single_tool` returns `(Message, state_effect)`; `run_tools` gains an optional `effects_sink`.
- `friday_agent/core/engine.py` — `compact()` carries `todos` forward (1 line).
- `docs/architecture/01-core-loop.md`, `02-tool-orchestration.md`, `07-data-models.md` — sync (architecture docs are the project's source of truth).

**Created:**
- `friday_agent/tools/builtin/todo_write.py` — the `TodoWrite` tool (declares the effect only).
- `tests/test_todo_tracking.py` — unit + integration + distributed-resume tests.
- `scripts/verify_todo.py` — real-API smoke test for the one thing fakes can't prove: that Anthropic accepts a user turn carrying a text block appended **after** `tool_result` blocks.

**Deliberately unchanged:** `friday_agent/tools/builtin/__init__.py` stays empty — tools are imported by module path (`from friday_agent.tools.builtin.todo_write import TodoWrite`), matching the existing `ExampleTool` pattern. No export added (YAGNI).

**Naming contract (used across tasks):**
- `LoopState.todos: list[dict]`, each item `{"content": str, "status": "pending"|"in_progress"|"completed"}`
- `ToolResult.state_effect: dict | None`
- `apply_state_effects(todos: list[dict], effects: list[dict]) -> list[dict]`
- `render_todo_reminder(todos: list[dict]) -> str`
- `with_todo_reminder(messages: list[Message], todos: list[dict]) -> list[Message]`
- `_run_single_tool(block, tools) -> tuple[Message, dict | None]`
- `run_tools(blocks, tools, *, max_concurrency=10, effects_sink: list[dict] | None = None)`
- `TodoWrite` / `TodoItem` / `TodoWriteInput`

---

## Task 1: `LoopState.todos` field + serde

**Files:**
- Modify: `friday_agent/core/state.py`
- Test: `tests/test_serialize.py`, `tests/test_state.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_serialize.py`:

```python
def test_loop_state_roundtrip_preserves_todos():
    todos = [{"content": "A", "status": "in_progress"}, {"content": "B", "status": "pending"}]
    s = LoopState(messages=_sample_messages(), turn_count=3, todos=todos)
    back = LoopState.from_dict(json.loads(json.dumps(s.to_dict(), ensure_ascii=False)))
    assert back.todos == todos


def test_loop_state_from_dict_without_todos_defaults_empty():
    # Legacy state dict (pre-todos) must not break.
    s = LoopState.from_dict({"messages": [], "turn_count": 2})
    assert s.todos == []
```

Add to `tests/test_state.py`:

```python
def test_loopstate_todos_defaults_empty():
    s = LoopState(messages=[create_user_message("hi")])
    assert s.todos == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_serialize.py::test_loop_state_roundtrip_preserves_todos tests/test_state.py::test_loopstate_todos_defaults_empty -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'todos'` / `AttributeError: 'LoopState' object has no attribute 'todos'`.

- [ ] **Step 3: Add the field + serde**

In `friday_agent/core/state.py`, change the import line:

```python
from dataclasses import dataclass, field
```

Replace the `LoopState` dataclass body (fields + both methods) with:

```python
@dataclass
class LoopState:
    """Serializable loop state, suitable for distributed resume.

    Carries messages, turn_count, and todos across iterations; run_one_turn()
    yields it directly as the "continue" sentinel at each turn boundary.
    todos is JSON-native structured task state, re-injected into each turn's
    API view as a reminder (see core/loop.py). Non-serializable runtime objects
    (provider/config) are intentionally excluded.
    """
    messages: list[Any]                             # list[Message] (full history)
    turn_count: int = 1
    todos: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "messages": [m.to_dict() for m in self.messages],
            "turn_count": self.turn_count,
            "todos": self.todos,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LoopState":
        return cls(
            messages=[Message.from_dict(m) for m in d.get("messages", [])],
            turn_count=d.get("turn_count", 1),
            todos=d.get("todos", []),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_serialize.py tests/test_state.py -v`
Expected: PASS (all, including the existing round-trip tests).

- [ ] **Step 5: Commit**

```bash
git add friday_agent/core/state.py tests/test_serialize.py tests/test_state.py
git commit -m "feat(state): add LoopState.todos field with serde + back-compat default"
```

---

## Task 2: `ToolResult.state_effect` field

**Files:**
- Modify: `friday_agent/tools/base.py`
- Test: `tests/test_tool_base.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_tool_base.py`:

```python
def test_tool_result_state_effect_default_and_set():
    assert ToolResult(data="ok").state_effect is None
    r = ToolResult(data="ok", state_effect={"todos": [{"content": "A", "status": "pending"}]})
    assert r.state_effect == {"todos": [{"content": "A", "status": "pending"}]}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_tool_base.py::test_tool_result_state_effect_default_and_set -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'state_effect'`.

- [ ] **Step 3: Add the field**

In `friday_agent/tools/base.py`, replace the `ToolResult` dataclass with:

```python
@dataclass
class ToolResult:
    """Result returned by a tool execution."""
    data: Any                          # execution result (string or structured data)
    is_error: bool = False
    state_effect: dict | None = None   # declarative loop-state mutation, e.g. {"todos": [...]}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_tool_base.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add friday_agent/tools/base.py tests/test_tool_base.py
git commit -m "feat(tools): add declarative ToolResult.state_effect channel"
```

---

## Task 3: `apply_state_effects` (loop's effect applier)

**Files:**
- Modify: `friday_agent/core/loop.py`
- Test: `tests/test_todo_tracking.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_todo_tracking.py`:

```python
"""Todo/Task tracking: state effects, reminder injection, distributed resume."""
import json

import pytest

from friday_agent.api.provider import (
    AssistantResponse, StopReason, TextBlock, ToolUseBlock, TokenUsage,
)
from friday_agent.core.engine import FridayAgent
from friday_agent.core.loop import apply_state_effects, render_todo_reminder, with_todo_reminder
from friday_agent.core.state import LoopState, Terminal
from friday_agent.messages.types import ContentBlock, create_user_message, create_tool_result_message
from friday_agent.tools.builtin.example_tool import ExampleTool
from friday_agent.tools.builtin.todo_write import TodoWrite
from tests._drive import collect_turn
from tests.fakes import FakeLLMProvider


def _api_text(api_messages: list[dict]) -> str:
    """Concatenate every text block across a normalized API message list."""
    parts = []
    for m in api_messages:
        for b in m["content"]:
            if b.get("type") == "text":
                parts.append(b.get("text", ""))
    return "\n".join(parts)


# --- apply_state_effects -----------------------------------------------------

def test_apply_state_effects_replaces_on_todos_effect():
    start = [{"content": "old", "status": "pending"}]
    new = [{"content": "new", "status": "in_progress"}]
    assert apply_state_effects(start, [{"todos": new}]) == new


def test_apply_state_effects_carry_forward_when_no_effect():
    start = [{"content": "keep", "status": "pending"}]
    assert apply_state_effects(start, []) == start


def test_apply_state_effects_last_write_wins():
    a = [{"content": "a", "status": "pending"}]
    b = [{"content": "b", "status": "completed"}]
    assert apply_state_effects([], [{"todos": a}, {"todos": b}]) == b


def test_apply_state_effects_ignores_unknown_effect_keys():
    start = [{"content": "keep", "status": "pending"}]
    assert apply_state_effects(start, [{"something_else": 1}]) == start
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_todo_tracking.py -k apply_state_effects -v`
Expected: FAIL — `ImportError: cannot import name 'apply_state_effects' from 'friday_agent.core.loop'`.

- [ ] **Step 3: Add `apply_state_effects`**

In `friday_agent/core/loop.py`, add this function after `yield_missing_tool_result_blocks` (around line 108) and before `run_one_turn`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_todo_tracking.py -k apply_state_effects -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add friday_agent/core/loop.py tests/test_todo_tracking.py
git commit -m "feat(loop): add apply_state_effects (loop-owned todos applier)"
```

---

## Task 4: `render_todo_reminder` + `with_todo_reminder`

**Files:**
- Modify: `friday_agent/core/loop.py`
- Test: `tests/test_todo_tracking.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_todo_tracking.py`:

```python
# --- render_todo_reminder ----------------------------------------------------

def test_render_todo_reminder_empty_is_blank():
    assert render_todo_reminder([]) == ""


def test_render_todo_reminder_formats_items():
    out = render_todo_reminder([
        {"content": "Wire it up", "status": "in_progress"},
        {"content": "Add tests", "status": "pending"},
    ])
    assert "<system-reminder>" in out and "</system-reminder>" in out
    assert "- [in_progress] Wire it up" in out
    assert "- [pending] Add tests" in out


# --- with_todo_reminder ------------------------------------------------------

def test_with_todo_reminder_joins_trailing_user_turn():
    todos = [{"content": "A", "status": "in_progress"}]
    original = [create_tool_result_message(tool_use_id="t1", result_text="ok")]  # role == "user"
    out = with_todo_reminder(original, todos)
    # No new consecutive user message: still one trailing user turn.
    assert len(out) == len(original)
    last = out[-1]
    assert last.role == "user"
    assert last.content[0].type == "tool_result"          # original block preserved, first
    assert last.content[-1].type == "text"                # reminder appended after
    assert "<system-reminder>" in last.content[-1].text
    # Input is not mutated (distributed-safe).
    assert len(original[-1].content) == 1


def test_with_todo_reminder_empty_todos_returns_unchanged():
    msgs = [create_user_message("hi")]
    assert with_todo_reminder(msgs, []) is msgs


def test_with_todo_reminder_appends_user_msg_when_trailing_not_user():
    todos = [{"content": "A", "status": "pending"}]
    assistant = create_user_message("x")
    assistant.role = "assistant"  # simulate a trailing non-user turn (defensive path)
    out = with_todo_reminder([assistant], todos)
    assert len(out) == 2 and out[-1].role == "user"
    assert "<system-reminder>" in out[-1].content[0].text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_todo_tracking.py -k "reminder" -v`
Expected: FAIL — `ImportError: cannot import name 'render_todo_reminder'`.

- [ ] **Step 3: Add the renderer + injector**

In `friday_agent/core/loop.py`, add `replace` to the dataclasses import near the top (after the `__future__` import):

```python
from dataclasses import replace
```

Add `create_user_message` to the existing `messages.types` import block so it reads:

```python
from friday_agent.messages.types import (
    ContentBlock,
    Message,
    create_tool_result_message,
    create_user_message,
)
```

Then add these two functions directly after `apply_state_effects`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_todo_tracking.py -k "reminder" -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add friday_agent/core/loop.py tests/test_todo_tracking.py
git commit -m "feat(loop): add render_todo_reminder + with_todo_reminder (turn-local injection)"
```

---

## Task 5: `TodoWrite` tool

**Files:**
- Create: `friday_agent/tools/builtin/todo_write.py`
- Test: `tests/test_todo_tracking.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_todo_tracking.py`:

```python
# --- TodoWrite tool ----------------------------------------------------------

@pytest.mark.asyncio
async def test_todowrite_returns_todos_state_effect():
    tool = TodoWrite()
    args = {"todos": [
        {"content": "A", "status": "in_progress"},
        {"content": "B", "status": "pending"},
    ]}
    result = await tool.call(args)
    assert result.is_error is False
    assert result.state_effect == {"todos": args["todos"]}
    assert "2" in str(result.data)


def test_todowrite_is_not_concurrency_safe():
    assert TodoWrite().is_concurrency_safe({}) is False


@pytest.mark.asyncio
async def test_todowrite_rejects_bad_status():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        await TodoWrite().call({"todos": [{"content": "A", "status": "not-a-status"}]})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_todo_tracking.py -k todowrite -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'friday_agent.tools.builtin.todo_write'`.

- [ ] **Step 3: Create the tool**

Create `friday_agent/tools/builtin/todo_write.py`:

```python
"""TodoWrite — structured task-list tool.

Declares a `todos` state_effect; the loop (the sole state writer) applies it.
The tool is intentionally stateless: it does not touch LoopState directly, so
parallel execution can never race on shared state. Reminder rendering lives in
core/loop.py (the loop owns the todos-state concept), not here.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from friday_agent.tools.base import Tool, ToolResult


class TodoItem(BaseModel):
    content: str = Field(description="Imperative task description, e.g. 'Add the login endpoint'")
    status: Literal["pending", "in_progress", "completed"] = Field(
        description="pending = not started, in_progress = active now, completed = done"
    )


class TodoWriteInput(BaseModel):
    todos: list[TodoItem] = Field(
        description="The COMPLETE updated list; it replaces the previous list entirely"
    )


class TodoWrite(Tool):
    """Create and manage a structured task list for the current session. Use for
    multi-step work (3+ steps) to track progress and show the user a plan. Send the
    ENTIRE updated list every call — it replaces the previous one. Keep exactly one
    item in_progress at a time; mark items completed the moment they are done."""

    name = "TodoWrite"

    def input_schema(self) -> type[BaseModel]:
        return TodoWriteInput

    def is_concurrency_safe(self, input_data: dict) -> bool:
        return False  # mutates tracked state -> never batched in parallel

    async def call(self, args: dict) -> ToolResult:
        todos = [t.model_dump() for t in TodoWriteInput(**args).todos]
        return ToolResult(
            data=f"Updated todo list ({len(todos)} items).",
            state_effect={"todos": todos},
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_todo_tracking.py -k todowrite -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add friday_agent/tools/builtin/todo_write.py tests/test_todo_tracking.py
git commit -m "feat(tools): add TodoWrite builtin (declares todos state_effect)"
```

---

## Task 6: Thread `state_effect` through the orchestrator

**Files:**
- Modify: `friday_agent/tools/orchestrator.py`
- Test: `tests/test_todo_tracking.py` (+ existing `tests/test_run_tools.py` must stay green)

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_todo_tracking.py`:

```python
# --- orchestrator effects_sink -----------------------------------------------

@pytest.mark.asyncio
async def test_run_tools_collects_state_effects_into_sink():
    from friday_agent.tools.orchestrator import run_tools
    blocks = [ContentBlock(type="tool_use", id="t1", name="TodoWrite",
                           input={"todos": [{"content": "A", "status": "pending"}]})]
    sink: list[dict] = []
    msgs = [m async for m in run_tools(blocks, [TodoWrite()], effects_sink=sink)]
    assert len(msgs) == 1 and msgs[0].content[0].type == "tool_result"
    assert sink == [{"todos": [{"content": "A", "status": "pending"}]}]


@pytest.mark.asyncio
async def test_run_tools_without_sink_still_yields_messages():
    from friday_agent.tools.orchestrator import run_tools
    blocks = [ContentBlock(type="tool_use", id="t1", name="ExampleTool",
                           input={"payload": "x", "mutating": False})]
    msgs = [m async for m in run_tools(blocks, [ExampleTool()])]
    assert len(msgs) == 1 and msgs[0].content[0].type == "tool_result"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_todo_tracking.py -k run_tools -v`
Expected: FAIL — `TypeError: run_tools() got an unexpected keyword argument 'effects_sink'`.

- [ ] **Step 3: Update `_run_single_tool` and `run_tools`**

In `friday_agent/tools/orchestrator.py`, replace `_run_single_tool` so it returns the effect alongside the message:

```python
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
        return create_tool_result_message(
            tool_use_id=block.id or "",
            result_text=str(result.data),
            is_error=result.is_error,
        ), result.state_effect
    except Exception as exc:
        return create_tool_result_message(
            tool_use_id=block.id or "",
            result_text=f"Error: {exc}",
            is_error=True,
        ), None
```

Replace `run_tools` with the `effects_sink` variant (block order preserved; effects appended in the same order as the yielded messages):

```python
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
```

- [ ] **Step 4: Run tests to verify they pass (new + regression)**

Run: `python -m pytest tests/test_todo_tracking.py -k run_tools tests/test_run_tools.py -v`
Expected: PASS — both new tests AND the existing `test_results_in_block_order_despite_parallel` (the message-yield contract is unchanged).

- [ ] **Step 5: Commit**

```bash
git add friday_agent/tools/orchestrator.py tests/test_todo_tracking.py
git commit -m "feat(orchestrator): collect ToolResult.state_effect via optional effects_sink"
```

---

## Task 7: Wire the reminder + effects into `run_one_turn`

**Files:**
- Modify: `friday_agent/core/loop.py`
- Test: `tests/test_todo_tracking.py`

- [ ] **Step 1: Write the failing integration tests**

Add to `tests/test_todo_tracking.py`:

```python
# --- loop integration --------------------------------------------------------

@pytest.mark.asyncio
async def test_todowrite_updates_next_state_and_reminder_appears_next_turn():
    todos = [{"content": "Wire it up", "status": "in_progress"},
             {"content": "Add tests", "status": "pending"}]
    r1 = AssistantResponse(
        content=[ToolUseBlock(id="t1", name="TodoWrite", input={"todos": todos})],
        stop_reason=StopReason.TOOL_USE, usage=TokenUsage(),
    )
    r2 = AssistantResponse(content=[TextBlock(text="done")], stop_reason=StopReason.END_TURN, usage=TokenUsage())
    fake = FakeLLMProvider(responses=[r1, r2])
    engine = FridayAgent(provider=fake, tools=[TodoWrite()])

    # Turn 1: todos empty at turn start -> NO reminder; effect lands in next state.
    _, outcome1 = await collect_turn(engine, LoopState(messages=[create_user_message("do multi-step work")]))
    assert isinstance(outcome1, LoopState)
    assert outcome1.todos == todos
    assert "<system-reminder>" not in _api_text(fake.received_messages[0])

    # Turn 2: reminder reflects committed todos, joined onto the trailing user (tool_result) turn.
    _, outcome2 = await collect_turn(engine, outcome1)
    assert isinstance(outcome2, Terminal) and outcome2.reason == "completed"
    turn2 = _api_text(fake.received_messages[1])
    assert "<system-reminder>" in turn2
    assert "[in_progress] Wire it up" in turn2


@pytest.mark.asyncio
async def test_reminder_never_leaks_into_persisted_state():
    preset = [{"content": "X", "status": "in_progress"}]
    r1 = AssistantResponse(
        content=[ToolUseBlock(id="t1", name="ExampleTool", input={"payload": "p", "mutating": False})],
        stop_reason=StopReason.TOOL_USE, usage=TokenUsage(),
    )
    fake = FakeLLMProvider(responses=[r1])
    engine = FridayAgent(provider=fake, tools=[ExampleTool()])

    _, outcome = await collect_turn(engine, LoopState(messages=[create_user_message("go")], todos=preset))
    assert isinstance(outcome, LoopState)
    # The API saw the reminder...
    assert "<system-reminder>" in _api_text(fake.received_messages[0])
    # ...but the persisted next state did NOT (built from clean state_messages).
    persisted = " ".join(b.text or "" for m in outcome.messages for b in m.content if b.type == "text")
    assert "<system-reminder>" not in persisted
    # ExampleTool emits no effect -> todos carried forward unchanged.
    assert outcome.todos == preset
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_todo_tracking.py -k "next_turn or leaks" -v`
Expected: FAIL — reminder absent from `received_messages[1]` (not yet wired); `outcome.todos` empty / reminder leaks.

- [ ] **Step 3: Wire `run_one_turn`**

In `friday_agent/core/loop.py`, inside `run_one_turn`, replace the message-prep block. Change:

```python
    state_messages = state.messages
    turn_count = state.turn_count

    # Caller-owned compaction: send the full state.messages; the caller keeps it
    # within the context window via engine.compact() on overflow.
    messages_for_query = list(state_messages)
```

to:

```python
    state_messages = state.messages
    turn_count = state.turn_count

    # API view only (turn-local, never persisted): inject the live todo reminder
    # so the model sees current progress without re-calling TodoWrite. The next
    # LoopState is assembled from the CLEAN state_messages below (no reminder leak).
    api_input_messages = list(state_messages)
    if state.todos:
        api_input_messages = with_todo_reminder(api_input_messages, state.todos)
```

Change the normalize line:

```python
    api_messages = normalize_for_api(messages_for_query)
```

to:

```python
    api_messages = normalize_for_api(api_input_messages)
```

Replace the tool-execution + continuation tail. Change:

```python
    # Execute all tool_use blocks, collecting results.
    async for result_msg in run_tools(tool_use_blocks, tools, max_concurrency=max_concurrency):
        tool_results.append(result_msg)
        yield result_msg

    next_turn_count = turn_count + 1

    # Continuation: yield the assembled next-turn LoopState.
    yield LoopState(
        messages=[*messages_for_query, *assistant_messages, *tool_results],
        turn_count=next_turn_count,
    )
```

to:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_todo_tracking.py tests/test_run_one_turn.py tests/test_distributed_resume.py -v`
Expected: PASS — new integration tests green AND existing loop/resume tests still green (with no todos, `api_input_messages == state_messages`, so behavior is identical to before).

- [ ] **Step 5: Commit**

```bash
git add friday_agent/core/loop.py tests/test_todo_tracking.py
git commit -m "feat(loop): inject per-turn todo reminder + carry todos into next LoopState"
```

---

## Task 8: `compact()` preserves todos

**Files:**
- Modify: `friday_agent/core/engine.py`
- Test: `tests/test_engine.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_engine.py`:

```python
@pytest.mark.asyncio
async def test_compact_preserves_todos():
    """Compaction happens in long conversations — exactly when todo anti-drift
    matters most — so todos must survive it (summary is prose; todos are state)."""
    summary = AssistantResponse(
        content=[TextBlock(text="<summary>S</summary>")],
        stop_reason=StopReason.END_TURN, usage=TokenUsage(),
    )
    engine = FridayAgent(provider=FakeLLMProvider(responses=[summary]), tools=[])
    todos = [{"content": "A", "status": "in_progress"}]
    state = LoopState(messages=[create_user_message("m1"), create_user_message("m2")],
                      turn_count=4, todos=todos)

    new_state = await engine.compact(state)

    assert len(new_state.messages) == 1
    assert new_state.turn_count == 4
    assert new_state.todos == todos
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_engine.py::test_compact_preserves_todos -v`
Expected: FAIL — `assert [] == [{'content': 'A', ...}]` (compact drops todos).

- [ ] **Step 3: Carry todos forward in `compact()`**

In `friday_agent/core/engine.py`, change the final return of `compact()`:

```python
        return LoopState(messages=[summary_message], turn_count=state.turn_count)
```

to:

```python
        return LoopState(
            messages=[summary_message],
            turn_count=state.turn_count,
            todos=state.todos,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_engine.py -v`
Expected: PASS (all engine tests, including the existing compact tests).

- [ ] **Step 5: Commit**

```bash
git add friday_agent/core/engine.py tests/test_engine.py
git commit -m "fix(engine): preserve todos across compact() (anti-drift survives long convos)"
```

---

## Task 9: Distributed-resume test for todos

**Files:**
- Test: `tests/test_todo_tracking.py`

- [ ] **Step 1: Write the test**

Add to `tests/test_todo_tracking.py`:

```python
# --- distributed resume ------------------------------------------------------

@pytest.mark.asyncio
async def test_todos_survive_serialize_roundtrip_and_reminder_regenerates():
    todos = [{"content": "A", "status": "in_progress"}]
    r1 = AssistantResponse(
        content=[ToolUseBlock(id="t1", name="TodoWrite", input={"todos": todos})],
        stop_reason=StopReason.TOOL_USE, usage=TokenUsage(),
    )
    r2 = AssistantResponse(content=[TextBlock(text="done")], stop_reason=StopReason.END_TURN, usage=TokenUsage())
    fake = FakeLLMProvider(responses=[r1, r2])
    engine = FridayAgent(provider=fake, tools=[TodoWrite()])

    # Worker A: run the TodoWrite turn, then serialize the resulting state.
    _, outcome1 = await collect_turn(engine, LoopState(messages=[create_user_message("go")]))
    assert isinstance(outcome1, LoopState)
    blob = json.dumps(outcome1.to_dict(), ensure_ascii=False)

    # Worker B: restore -> todos preserved -> resume regenerates the same reminder.
    restored = LoopState.from_dict(json.loads(blob))
    assert restored.todos == todos
    _, outcome2 = await collect_turn(engine, restored)
    assert isinstance(outcome2, Terminal) and outcome2.reason == "completed"
    assert "<system-reminder>" in _api_text(fake.received_messages[1])
    assert "[in_progress] A" in _api_text(fake.received_messages[1])
```

- [ ] **Step 2: Run test to verify it passes**

Run: `python -m pytest tests/test_todo_tracking.py::test_todos_survive_serialize_roundtrip_and_reminder_regenerates -v`
Expected: PASS (all prior tasks already make this work; this is the integrated proof).

- [ ] **Step 3: Run the whole new suite**

Run: `python -m pytest tests/test_todo_tracking.py -v`
Expected: PASS (all todo-tracking tests).

- [ ] **Step 4: Commit**

```bash
git add tests/test_todo_tracking.py
git commit -m "test(todo): prove todos survive distributed-resume serialize boundary"
```

---

## Task 10: Real-API smoke test (`scripts/verify_todo.py`)

**Files:**
- Create: `scripts/verify_todo.py`

Why: fake-provider tests can't confirm the one vendor-contract risk — that Anthropic accepts a user turn whose content is `[tool_result, ..., text]` (the reminder appended **after** the tool_result block). This script forces exactly that shape: turn 1 calls `TodoWrite` (todos become non-empty), so turn 2 starts with the trailing message being `TodoWrite`'s tool_result AND non-empty todos → the reminder is appended after it and sent to the API. Reaching `completed` proves acceptance; the recording wrapper proves the reminder was actually in that payload.

- [ ] **Step 1: Create the script**

Create `scripts/verify_todo.py`:

```python
"""verify_todo.py — Real-API smoke test for the todo reminder injection.

Confirms the one thing fake-provider tests cannot: that the backend accepts a
user turn carrying a text block appended AFTER tool_result blocks (the reminder
joined onto a tool_result turn).

Usage:
    LLM_MODEL=<model-id> python scripts/verify_todo.py

Cost guardrail: max_tokens=512; caller-side turn cap=6.
"""
from __future__ import annotations

import asyncio
import os
import sys

from _env import create_provider, resolve_api_key
from friday_agent.core.engine import FridayAgent
from friday_agent.core.state import LoopState, Terminal
from friday_agent.messages.types import Message, create_user_message
from friday_agent.tools.builtin.example_tool import ExampleTool
from friday_agent.tools.builtin.todo_write import TodoWrite


class _Recording:
    """Duck-typed LLMProvider wrapper that captures each complete() call's messages."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.config_type = inner.config_type
        self.received_messages: list[list[dict]] = []

    async def complete(self, messages, system_prompt, tools, config):
        self.received_messages.append(messages)
        return await self._inner.complete(
            messages=messages, system_prompt=system_prompt, tools=tools, config=config
        )


def _reminder_after_tool_result(api_messages: list[dict]) -> bool:
    """True if some user message contains a tool_result block followed by a
    <system-reminder> text block (the exact shape we need the API to accept)."""
    for m in api_messages:
        if m.get("role") != "user":
            continue
        types = [b.get("type") for b in m["content"]]
        if "tool_result" not in types:
            continue
        for b in m["content"]:
            if b.get("type") == "text" and "<system-reminder>" in (b.get("text") or ""):
                return True
    return False


async def main() -> int:
    print("=" * 60)
    print("Verification: Todo reminder injection (Real API)")
    print("=" * 60)

    model = os.environ.get("LLM_MODEL", "")
    if not model:
        sys.exit("Set the LLM_MODEL environment variable to a real model ID.")

    provider = _Recording(create_provider(model, api_key=resolve_api_key(model)))
    config = provider.config_type(max_tokens=512)
    system_prompt = (
        "You are a helpful assistant doing multi-step work. "
        "FIRST call TodoWrite with a 2-item list (one in_progress, one pending). "
        "THEN call ExampleTool once to process the text 'hello'. "
        "THEN mark both todos completed via TodoWrite and give a one-sentence summary. Stop."
    )
    engine = FridayAgent(provider=provider, tools=[TodoWrite(), ExampleTool()],
                         system_prompt=system_prompt, config=config)

    user_message = "Please complete the two-step task and track it with todos."
    print(f"\nUser: {user_message}\n")

    _CAP = 6
    state = LoopState(messages=[create_user_message(user_message)])
    collected: list[Message] = []
    terminal: Terminal | None = None
    todos_seen_nonempty = False
    turns = 0
    while True:
        outcome = None
        async for item in engine.step(state):
            if isinstance(item, (LoopState, Terminal)):
                outcome = item
            else:
                collected.append(item)
        turns += 1
        if isinstance(outcome, Terminal):
            terminal = outcome
            break
        state = outcome
        if state.todos:
            todos_seen_nonempty = True
        if turns >= _CAP:
            break

    todowrite_called = any(
        b.type == "tool_use" and b.name == "TodoWrite" for m in collected for b in m.content
    )
    reminder_shape_sent = any(_reminder_after_tool_result(msgs) for msgs in provider.received_messages)

    print(f"\n--- Terminal ---")
    print(f"  reason    : {terminal.reason if terminal else f'(turn cap {turns} reached)'}")
    print(f"  api_calls : {len(provider.received_messages)}")
    if terminal and terminal.error:
        print(f"  error     : {terminal.error}")

    print("\n--- Checklist ---")
    checks = {
        "TodoWrite was called": todowrite_called,
        "todos became non-empty in state": todos_seen_nonempty,
        "reminder sent appended-after-tool_result (>=1 call)": reminder_shape_sent,
        "API accepted reminder turn (reason == 'completed')":
            terminal is not None and terminal.reason == "completed",
    }
    all_pass = True
    for label, ok in checks.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        all_pass = all_pass and ok

    print("\n" + ("=" * 20 + " PASS " + "=" * 20 if all_pass else "=" * 20 + " FAIL " + "=" * 20))
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```

- [ ] **Step 2: Syntax-check (no key needed)**

Run: `python -m py_compile scripts/verify_todo.py`
Expected: no output (exit 0).

- [ ] **Step 3: (Optional, costs tokens) run against a real model**

Run: `LLM_MODEL=claude-sonnet-4-6 python scripts/verify_todo.py`
Expected: `==== PASS ====` with all four checks green. Skip if no API key is configured; the py_compile check is the gate for this plan.

- [ ] **Step 4: Commit**

```bash
git add scripts/verify_todo.py
git commit -m "test(verify): add real-API smoke test for todo reminder ordering"
```

---

## Task 11: Sync architecture docs (source of truth)

**Files:**
- Modify: `docs/architecture/07-data-models.md`, `docs/architecture/02-tool-orchestration.md`, `docs/architecture/01-core-loop.md`

- [ ] **Step 1: Update `07-data-models.md`**

In the §① serialization-boundary table, change the `LoopState` row text `루프 상태(messages + turn_count)` to `루프 상태(messages + turn_count + todos)`.

In §2.2, replace the `LoopState` field table with:

```markdown
| 필드 | 타입 | 의미 |
|---|---|---|
| `messages` | `list[Message]` | 전체 히스토리 |
| `turn_count` | `int` (기본 1) | 턴 카운터 |
| `todos` | `list[dict]` (기본 `[]`) | 추적 중인 todo 목록; 각 항목 `{"content", "status"}`. 매 턴 API view에 리마인더로 주입(비영속) |
```

In §2.5, replace the `ToolResult` table with:

```markdown
| 필드 | 타입 | 의미 |
|---|---|---|
| `data` | `Any` | 실행 결과 (문자열/구조화 데이터) |
| `is_error` | `bool` | 오류 여부 (기본 `False`) |
| `state_effect` | `dict \| None` | 선언적 루프-상태 변이(예 `{"todos": [...]}`); 루프가 적용, 기본 `None` |
```

- [ ] **Step 2: Update `02-tool-orchestration.md`**

In §④, replace the `ToolResult` code block:

```python
ToolResult(
    data,                   # 실행 결과 (문자열 또는 구조화 데이터)
    is_error=False,
    state_effect=None,      # 선언적 상태 변이(예 {"todos": [...]}); 루프가 단독 적용
)
```

In §③, after the `run_tools` 동작 list (after item 4), add:

```markdown
5. `effects_sink`(선택 인자)가 주어지면, 각 도구의 `ToolResult.state_effect`(None 아님)를 **블록 순서대로** sink에 누적한다. 메시지 스트림은 불변이며, `_run_single_tool`은 `(Message, state_effect)` 튜플을 반환한다. 루프가 이 sink를 모아 다음 `LoopState.todos`를 계산한다(상태 단독 writer = 루프).
```

- [ ] **Step 3: Update `01-core-loop.md`**

In §④ 상태 타입 table, change the `LoopState` row to:

```markdown
| `LoopState(messages, turn_count=1, todos=[])` | `core/state.py:39` | 직렬화 가능한 루프 이송 단위 + 턴 경계 "계속" 재개 sentinel |
```

In §⑥ 유지보수 주의점, change the serde bullet `messages + turn_count만 왕복한다` to `messages + turn_count + todos만 왕복한다`, and add this bullet:

```markdown
- **턴별 todo 리마인더는 비영속.** `state.todos`가 비어있지 않으면 `run_one_turn()`이 매 턴 `<system-reminder>`를 **API view 전용 사본(`api_input_messages`)**의 끝 user 턴에 합류시켜 전송한다. 다음 `LoopState`는 리마인더가 없는 `state_messages`에서 조립되므로 리마인더는 상태에 쌓이지 않고, 분산 재개 시 `todos`에서 결정론적으로 재생성된다. `engine.compact()`도 `todos`를 carry-forward한다(요약은 prose, todos는 구조화 상태).
```

- [ ] **Step 4: Verify no stale references and docs build sanity**

Run: `rg -n 'state_effect|todos|api_input_messages' docs/architecture/01-core-loop.md docs/architecture/02-tool-orchestration.md docs/architecture/07-data-models.md`
Expected: matches present in all three files (confirming the edits landed).

- [ ] **Step 5: Commit**

```bash
git add docs/architecture/01-core-loop.md docs/architecture/02-tool-orchestration.md docs/architecture/07-data-models.md
git commit -m "docs(arch): document todos state, state_effect channel, and per-turn reminder"
```

---

## Task 12: Full-suite green + final verification

**Files:** none (verification only)

- [ ] **Step 1: Run the entire test suite (keyless)**

Run: `python -m pytest -q`
Expected: PASS — all prior tests plus the new `tests/test_todo_tracking.py` and `test_compact_preserves_todos`. No failures, no errors.

- [ ] **Step 2: Syntax-check all scripts**

Run: `python -m py_compile scripts/run_agent.py scripts/verify_p2.py scripts/verify_p3.py scripts/verify_p4.py scripts/verify_todo.py`
Expected: no output (exit 0).

- [ ] **Step 3: Confirm the loop imports nothing from builtin tools (decoupling invariant)**

Run: `rg -n 'tools\.builtin' friday_agent/core/loop.py || echo "OK: core/loop.py does not import builtin tools"`
Expected: `OK: core/loop.py does not import builtin tools`.

- [ ] **Step 4: Final commit (if any uncommitted verification artifacts)**

```bash
git status --short
# expected: clean (everything committed task-by-task)
```

---

## Self-Review

**Spec coverage** (`docs/proposals/2026-06-01-todo-tracking-design.md`):
- §4.1 `LoopState.todos` + serde → Task 1. ✓
- §4.2 `ToolResult.state_effect` + `apply_state_effects` → Tasks 2, 3. ✓
- §4.3 `TodoWrite` tool → Task 5. ✓
- §4.4 reminder injection (`render_todo_reminder`/`with_todo_reminder`, `api_input_messages`) → Tasks 4, 7. ✓
- §4.5 effect collection (`_run_single_tool` tuple + `effects_sink`) + next `LoopState` from clean `state_messages` → Tasks 6, 7. ✓
- §6 invariants (pairing, role alternation, distributed resume, no race, back-compat) → covered by tests in Tasks 4, 7, 9 + `test_loop_state_from_dict_without_todos_defaults_empty`. ✓
- §7 test strategy (unit + integration + distributed) → Tasks 1–9. ✓
- §8 touch map incl. `compact()` carry-forward (1 line) → Task 8; `builtin/__init__.py` deliberately unchanged (documented). ✓
- §8 docs sync (01/02/07) → Task 11. ✓
- Real-API ordering check (text after tool_result) → Task 10. ✓

**Placeholder scan:** No TBD/TODO/"handle edge cases"; every code step shows complete code; every command lists expected output.

**Type consistency:** `apply_state_effects`/`render_todo_reminder`/`with_todo_reminder`/`run_tools(effects_sink=)`/`_run_single_tool -> tuple`/`TodoWrite`/`LoopState.todos`/`ToolResult.state_effect` names are used identically in every task that references them. `is_concurrency_safe(self, input_data)` matches the call site (positional, `parsed.model_dump()`).
