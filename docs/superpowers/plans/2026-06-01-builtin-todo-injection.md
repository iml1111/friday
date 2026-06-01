# Built-in TodoWrite Tool & Guidance Injection — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `TodoWrite` and its usage guidance always present in `FridayAgent` — auto-registered and auto-injected with no caller wiring and no opt-out — and reject any caller-supplied tool that collides with a built-in name.

**Architecture:** Inject at the engine/prompt boundary only (the core loop stays decoupled from tool names). `FridayAgent.__init__` merges `builtin_tools()` into the caller's tools, raising `ValueError` on a name collision; `assemble_system_prompt()` appends a new always-on `TODO_GUIDANCE` block beside `GENERAL_AGENT_GUIDANCE`. The per-turn `<system-reminder>` (already always-on) is unchanged.

**Tech Stack:** Python 3.11+, pydantic, anyio/asyncio, pytest + pytest-asyncio. Tests run keyless via `tests/fakes.py::FakeLLMProvider`.

**Authoritative spec:** `docs/proposals/2026-06-01-builtin-todo-injection-design.md`.

**Commit convention (every task):** commit directly to the current branch (`main`) with **explicit `git add <paths>`** — never `git add -A`/`git add .` (the repo carries untracked files). End each commit message with:
```
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
```
Before the first commit, confirm the branch: `git rev-parse --abbrev-ref HEAD` → expect `main`.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `friday_agent/tools/builtin/__init__.py` | `builtin_tools()` accessor — the single source of always-registered tools | 1 |
| `friday_agent/core/engine.py` | Merge built-ins into `self._tools` + reject name collisions (`__init__`) | 2 |
| `friday_agent/api/prompts.py` | `TODO_GUIDANCE` constant + append it in `assemble_system_prompt` | 3 |
| `scripts/run_agent.py` | Drop now-redundant explicit `TodoWrite()`/guidance (else it errors) | 5 |
| `tests/test_builtin_injection.py` (new) | Unit + integration tests for auto-registration & collision | 1,2,4 |
| `tests/test_prompts.py` | Guidance-injection tests (+ fix the now-breaking equality test) | 3 |
| `tests/test_engine.py` | Assert `compact()` path excludes `TODO_GUIDANCE` | 3 |
| `docs/architecture/01-core-loop.md`, `02-tool-orchestration.md`, `CLAUDE.md` | SSOT sync | 6 |

> **Cross-task import discipline (learned the hard way):** `tests/test_builtin_injection.py` is created in Task 1 with a *minimal* import header and grows in Task 2. It must **never** import `TODO_GUIDANCE` (that lives only in `test_prompts.py`/`test_engine.py`, Task 3), so its import set is final after Task 2. Each task below states exactly which imports to add.

---

## Task 1: `builtin_tools()` accessor

**Files:**
- Modify: `friday_agent/tools/builtin/__init__.py` (currently empty)
- Test: `tests/test_builtin_injection.py` (create)

- [ ] **Step 1: Write the failing test** — create `tests/test_builtin_injection.py` with this exact minimal header (only symbols that exist after this task):

```python
"""Tests for SDK built-in tool auto-registration and guidance (always-on, no opt-out).

Import discipline: this file must NOT import TODO_GUIDANCE — guidance assertions
live in tests/test_prompts.py and tests/test_engine.py. Keep imports to what the
engine/tools layer exposes.
"""
import pytest

from friday_agent.tools.builtin import builtin_tools
from friday_agent.tools.builtin.todo_write import TodoWrite


def test_builtin_tools_includes_todo_write():
    tools = builtin_tools()
    assert any(isinstance(t, TodoWrite) for t in tools)
    assert any(t.name == "TodoWrite" for t in tools)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_builtin_injection.py -v`
Expected: FAIL — `ImportError: cannot import name 'builtin_tools' from 'friday_agent.tools.builtin'`.

- [ ] **Step 3: Write minimal implementation** — replace the empty `friday_agent/tools/builtin/__init__.py` with:

```python
"""Built-in tools that FridayAgent always registers, even without caller injection.

Populated intentionally (it was previously empty): todo tracking is now an
always-on SDK capability, so its tool ships built-in rather than caller-wired.
"""
from __future__ import annotations

from friday_agent.tools.base import Tool
from friday_agent.tools.builtin.todo_write import TodoWrite


def builtin_tools() -> list[Tool]:
    """Return fresh instances of the tools FridayAgent registers unconditionally."""
    return [TodoWrite()]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_builtin_injection.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add friday_agent/tools/builtin/__init__.py tests/test_builtin_injection.py
git commit -m "feat: add builtin_tools() accessor (always-registered TodoWrite)"
```

---

## Task 2: `FridayAgent` merges built-ins and rejects name collisions

**Files:**
- Modify: `friday_agent/core/engine.py` (imports at top; `__init__` body, currently `friday_agent/core/engine.py:45-67`)
- Test: `tests/test_builtin_injection.py` (add imports + 3 tests)

- [ ] **Step 1: Write the failing tests** — in `tests/test_builtin_injection.py`, extend the import block to (this is the FINAL import set for this file):

```python
import pytest

from friday_agent.api.provider import (
    AssistantResponse,
    StopReason,
    TextBlock,
    TokenUsage,
    ToolUseBlock,
)
from friday_agent.core.engine import FridayAgent
from friday_agent.core.state import LoopState
from friday_agent.messages.types import create_user_message
from friday_agent.tools.builtin import builtin_tools
from friday_agent.tools.builtin.todo_write import TodoWrite
from friday_agent.tools.builtin.example_tool import ExampleTool
from tests._drive import collect_turn
from tests.fakes import FakeLLMProvider
```

Then append these tests:

```python
@pytest.mark.asyncio
async def test_step_registers_todo_write_without_caller_injection():
    """tools=[] still exposes TodoWrite to the provider (auto-registered)."""
    end = AssistantResponse(content=[TextBlock(text="ok")], stop_reason=StopReason.END_TURN, usage=TokenUsage())
    fake = FakeLLMProvider(responses=[end])
    engine = FridayAgent(provider=fake, tools=[])
    await collect_turn(engine, LoopState(messages=[create_user_message("hi")]))
    names = {t["name"] for t in fake.received_tools[0]}
    assert "TodoWrite" in names


@pytest.mark.asyncio
async def test_step_keeps_caller_tools_and_adds_builtins():
    """Caller tools are preserved AND built-ins are added."""
    end = AssistantResponse(content=[TextBlock(text="ok")], stop_reason=StopReason.END_TURN, usage=TokenUsage())
    fake = FakeLLMProvider(responses=[end])
    engine = FridayAgent(provider=fake, tools=[ExampleTool()])
    await collect_turn(engine, LoopState(messages=[create_user_message("hi")]))
    names = {t["name"] for t in fake.received_tools[0]}
    assert {"ExampleTool", "TodoWrite"} <= names


def test_caller_injecting_builtin_name_raises():
    """Passing a tool whose name collides with a built-in is rejected at construction."""
    with pytest.raises(ValueError, match="TodoWrite"):
        FridayAgent(provider=FakeLLMProvider(responses=[]), tools=[TodoWrite()])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_builtin_injection.py -v`
Expected: the two `step` tests FAIL (`"TodoWrite"` not in `names`); `test_caller_injecting_builtin_name_raises` FAILS (no `ValueError` raised — `tools=[TodoWrite()]` is currently accepted).

- [ ] **Step 3: Write minimal implementation** — in `friday_agent/core/engine.py`:

Add the import (with the other `friday_agent.tools` import near `friday_agent/core/engine.py:19`):

```python
from friday_agent.tools.builtin import builtin_tools
```

Replace the tool-assignment line in `__init__` (currently `self._tools = tools if tools is not None else []`) with the merge-and-validate block:

```python
        caller_tools = tools if tools is not None else []
        builtins = builtin_tools()
        builtin_names = {t.name for t in builtins}
        clash = sorted({t.name for t in caller_tools if t.name in builtin_names})
        if clash:
            raise ValueError(
                f"FridayAgent: {clash} are built-in tools that are always registered; "
                f"remove them from the tools list."
            )
        self._tools = [*caller_tools, *builtins]
```

(Leave the surrounding lines — `self._provider`, `self._system_prompt`, `self._config`, `self._max_concurrency` — unchanged.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_builtin_injection.py -v`
Expected: all PASS.

- [ ] **Step 5: Run the full suite (no regressions in engine/tool tests)**

Run: `python -m pytest -q`
Expected: all PASS (Task 3 has not touched prompts yet, so nothing here breaks).

- [ ] **Step 6: Commit**

```bash
git add friday_agent/core/engine.py tests/test_builtin_injection.py
git commit -m "feat: FridayAgent auto-registers builtins; reject caller name collisions"
```

---

## Task 3: `TODO_GUIDANCE` always injected into the system prompt

**Files:**
- Modify: `friday_agent/api/prompts.py` (add constant near `friday_agent/api/prompts.py:26`; rewrite `assemble_system_prompt` at `friday_agent/api/prompts.py:50-68`)
- Test: `tests/test_prompts.py` (add import + tests; fix the breaking equality test)
- Test: `tests/test_engine.py` (assert compact path excludes guidance)

- [ ] **Step 1: Write/Update the failing tests** — in `tests/test_prompts.py`, add `TODO_GUIDANCE` to the import:

```python
from friday_agent.api.prompts import (
    GENERAL_AGENT_GUIDANCE,
    TODO_GUIDANCE,
    assemble_system_prompt,
)
```

Replace the existing `test_assemble_empty_base_returns_guidance_only` (it asserts equality with `GENERAL_AGENT_GUIDANCE` alone, which is now wrong) with:

```python
def test_assemble_empty_base_returns_general_then_todo_guidance():
    s = str(assemble_system_prompt(""))
    assert s == f"{GENERAL_AGENT_GUIDANCE}\n\n{TODO_GUIDANCE}"
```

Append a new test:

```python
def test_assemble_always_injects_todo_guidance():
    assert TODO_GUIDANCE in str(assemble_system_prompt("You are a research assistant."))
    assert TODO_GUIDANCE in str(assemble_system_prompt(""))
    # Base prompt still leads; general guidance still present.
    s = str(assemble_system_prompt("BASE"))
    assert s.startswith("BASE")
    assert GENERAL_AGENT_GUIDANCE in s
```

In `tests/test_engine.py`, add `TODO_GUIDANCE` to the prompts import (currently `from friday_agent.api.prompts import GENERAL_AGENT_GUIDANCE`):

```python
from friday_agent.api.prompts import GENERAL_AGENT_GUIDANCE, TODO_GUIDANCE
```

and add one assertion inside the existing `test_compact_does_not_inject_general_guidance` (after the existing asserts):

```python
    assert TODO_GUIDANCE not in fake.received_system_prompts[0]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_prompts.py tests/test_engine.py -v`
Expected: FAIL — `ImportError: cannot import name 'TODO_GUIDANCE'`.

- [ ] **Step 3: Write minimal implementation** — in `friday_agent/api/prompts.py`, add the constant after `GENERAL_AGENT_GUIDANCE`'s closing triple-quote (around `friday_agent/api/prompts.py:47`):

```python
TODO_GUIDANCE: str = """# Task tracking
You have a TodoWrite tool for tracking multi-step work. For any task with several
steps, call TodoWrite first to lay out the plan, then keep it updated as you go.
 - Always send the COMPLETE list each call; it replaces the previous one.
 - Keep exactly one item in_progress at a time; mark items completed the moment they are done.
 - Skip it for trivial single-step tasks.
The current list is surfaced to you each turn inside a <system-reminder>; it reflects tracked state, not necessarily the user's latest instruction."""
```

Replace the body of `assemble_system_prompt` (the `if system_prompt: ... else: ...` block and `return`) with:

```python
    blocks = [b for b in (system_prompt, GENERAL_AGENT_GUIDANCE, TODO_GUIDANCE) if b]
    return SystemPrompt(text="\n\n".join(blocks))
```

(Update the docstring's "Injects the always-on general agent guidance" line to mention the todo guidance too — keep it one sentence.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_prompts.py tests/test_engine.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add friday_agent/api/prompts.py tests/test_prompts.py tests/test_engine.py
git commit -m "feat: always inject TODO_GUIDANCE into the system prompt"
```

---

## Task 4: Integration — todo tracking works with zero caller wiring

**Files:**
- Test: `tests/test_builtin_injection.py` (append one test; imports already final from Task 2)

- [ ] **Step 1: Write the test** — append to `tests/test_builtin_injection.py`:

```python
@pytest.mark.asyncio
async def test_todo_write_callable_with_zero_caller_wiring():
    """No caller tools at all: the model can still call TodoWrite and LoopState.todos updates."""
    tool_use = AssistantResponse(
        content=[ToolUseBlock(
            id="t1", name="TodoWrite",
            input={"todos": [{"content": "A", "status": "in_progress"}]},
        )],
        stop_reason=StopReason.TOOL_USE, usage=TokenUsage(),
    )
    end = AssistantResponse(content=[TextBlock(text="done")], stop_reason=StopReason.END_TURN, usage=TokenUsage())
    engine = FridayAgent(provider=FakeLLMProvider(responses=[tool_use, end]), tools=[])

    _, outcome = await collect_turn(engine, LoopState(messages=[create_user_message("plan it")]))

    assert isinstance(outcome, LoopState)
    assert outcome.todos == [{"content": "A", "status": "in_progress"}]
```

- [ ] **Step 2: Run the test to verify it passes** (the implementation already exists from Tasks 1–3, so this is a green-from-the-start integration check):

Run: `python -m pytest tests/test_builtin_injection.py::test_todo_write_callable_with_zero_caller_wiring -v`
Expected: PASS. (If it fails, the wiring from Task 2 is wrong — fix there, not here.)

- [ ] **Step 3: Commit**

```bash
git add tests/test_builtin_injection.py
git commit -m "test: TodoWrite callable end-to-end with zero caller wiring"
```

---

## Task 5: Simplify `scripts/run_agent.py` (mandatory — else it now errors)

**Why mandatory:** with Task 2 in place, passing `TodoWrite()` raises `ValueError`. The example must rely on auto-registration instead.

**Files:**
- Modify: `scripts/run_agent.py`

- [ ] **Step 1: Remove the explicit import** — delete this line (currently `scripts/run_agent.py:47`):

```python
from friday_agent.tools.builtin.todo_write import TodoWrite
```

- [ ] **Step 2: Drop `TodoWrite()` from the tools list** — change:

```python
        tools=[ExampleTool(), TodoWrite()],
```
to:
```python
        tools=[ExampleTool()],   # TodoWrite is auto-registered by FridayAgent (built-in)
```

- [ ] **Step 3: Remove the hand-written todo sentence from the system prompt** — change `_SYSTEM_PROMPT` back to (drop the "For any task with multiple steps…" sentence; guidance is now auto-injected):

```python
_SYSTEM_PROMPT = (
    "You are a helpful assistant. "
    "When the user asks you to process some text, call ExampleTool with that text "
    "as the payload, then report the tool's result."
)
```

- [ ] **Step 4: Update the banner line** — change:

```python
    print(f"friday-agent local example — model={_MODEL}  (tools: ExampleTool, TodoWrite)")
```
to:
```python
    print(f"friday-agent local example — model={_MODEL}  (tools: ExampleTool; TodoWrite is built-in)")
```

- [ ] **Step 5: Update the module docstring** — change the "TodoWrite is also wired in to demonstrate todo/task tracking." paragraph to reflect that it is now built-in (no caller wiring), e.g. replace its first sentence with:

```
TodoWrite is a built-in tool: FridayAgent auto-registers it and auto-injects its
usage guidance, so the caller wires up nothing. For multi-step work the model calls
TodoWrite; the tracked list is re-injected into every turn's API view as a
<system-reminder> and is persisted here across user inputs (seeded into each turn's
LoopState.todos). The live list is printed under a "── todo list ──" header whenever
it changes so you can watch progress.
```

(Keep everything else: `_render_todos`, the `TodoWrite` special-case in `_print_new_messages`, and the cross-input `todos` persistence — these remain caller-side concerns.)

- [ ] **Step 6: Verify it compiles** (no API key needed; interactive script can't be run keyless):

Run: `python -m py_compile scripts/run_agent.py`
Expected: no output, exit 0.

- [ ] **Step 7: Commit**

```bash
git add scripts/run_agent.py
git commit -m "refactor: run_agent relies on built-in TodoWrite (drop explicit wiring)"
```

---

## Task 6: Sync architecture docs + CLAUDE.md

**Files:**
- Modify: `docs/architecture/01-core-loop.md` (§⑥ maintenance notes)
- Modify: `docs/architecture/02-tool-orchestration.md` (§⑥ maintenance notes)
- Modify: `CLAUDE.md` (public-API description)

- [ ] **Step 1: `docs/architecture/01-core-loop.md`** — in §⑥ Maintenance Notes, after the "**General behavior block auto-injection.**" bullet, add a new bullet:

```
- **TodoWrite tool · guidance auto-injection (built-in).** `FridayAgent` always merges the tools from `builtin_tools()` (`tools/builtin/__init__.py`) into the caller's tools, and `assemble_system_prompt()` always appends `TODO_GUIDANCE` (no opt-out). If the caller injects a tool with the same name as a built-in, `FridayAgent.__init__` rejects it with `ValueError`. The compaction summary does not go through this prompt path, so `TODO_GUIDANCE` does not leak into the summary.
```

- [ ] **Step 2: `docs/architecture/02-tool-orchestration.md`** — in §⑥ Maintenance Notes, add a new note paragraph:

```
**Built-in tool auto-registration** — `FridayAgent` always merges the tools returned by `builtin_tools()` (`friday_agent/tools/builtin/__init__.py`) (currently `TodoWrite`) after the caller's tools. If the caller passes a tool with a clashing name, `__init__` rejects it with `ValueError` (the LLM API rejects duplicate tool names, so integrity is kept via explicit rejection rather than silent dedupe). Injection happens only at the engine boundary, so the orchestrator · loop remain unaware of tool names.
```

- [ ] **Step 3: `CLAUDE.md`** — in `## Target Stack & Verification`, append one sentence to the public-API paragraph (after the sentence ending "...`FridayAgent(...)`."):

```
The `TodoWrite` tool and todo guidance (`TODO_GUIDANCE`) are **always auto-registered·injected as built-ins** (no caller injection needed, no opt-out). If the caller passes a tool with the same name, `FridayAgent.__init__` rejects it with `ValueError`.
```

- [ ] **Step 4: Verify no stray references and the suite is green**

Run: `python -m pytest -q`
Expected: all PASS (count = prior total + the new builtin-injection tests).

- [ ] **Step 5: Commit**

```bash
git add docs/architecture/01-core-loop.md docs/architecture/02-tool-orchestration.md CLAUDE.md
git commit -m "docs: document always-on built-in TodoWrite + guidance injection"
```

---

## Final Verification

1. **Full suite (keyless):**
   ```bash
   python -m pytest -q
   ```
   Expected: all PASS. New tests: `tests/test_builtin_injection.py` (4 tests), `tests/test_prompts.py` (+1, 1 renamed), `tests/test_engine.py` (+1 assertion).
2. **Script compiles:**
   ```bash
   python -m py_compile scripts/run_agent.py
   ```
3. **No caller can shadow the built-in (manual smoke, keyless):**
   ```bash
   python -c "from friday_agent.core.engine import FridayAgent; from friday_agent.tools.builtin.todo_write import TodoWrite; from tests.fakes import FakeLLMProvider; \
   import sys; \
   exec('try:\n FridayAgent(provider=FakeLLMProvider(responses=[]), tools=[TodoWrite()])\n print(\"NO ERROR — BUG\"); sys.exit(1)\nexcept ValueError as e:\n print(\"OK:\", e)')"
   ```
   Expected: `OK: FridayAgent: ['TodoWrite'] are built-in tools ...`.
4. **(Optional, needs API key — token cost)** interactive run; confirm `TodoWrite` is callable and the todo list renders without any caller wiring:
   ```bash
   python scripts/run_agent.py
   # > Plan and do a 3-step task: A, then B, then C.
   ```
