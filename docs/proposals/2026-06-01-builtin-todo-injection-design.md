# Design Proposal: Always-Injecting the TodoWrite Tool · Guidance (Making It Built-in)

- **Date**: 2026-06-01
- **Target**: `friday_agent` (distributed agent loop SDK)
- **Status**: design approved · not implemented
- **Prerequisite**: [`2026-06-01-todo-tracking-design.md`](2026-06-01-todo-tracking-design.md) — assumes the todo tracking mechanism (LoopState.todos · state_effect · per-turn reminder) has already been implemented. This document promotes that feature to a **built-in that is always on, with no caller injection**.

---

## 1. Background & Motivation

Todo tracking is already implemented, but currently it works only if **the caller wires it up directly**:

- The `TodoWrite` tool must be put directly into `FridayAgent(tools=[...])` for the model to be able to call it.
- The caller must hand-write guidance on when · how to use `TodoWrite` (concept · protocol) into its own `system_prompt` (as `scripts/run_agent.py` currently does).

That is, if the caller omits either, the feature is effectively dead. Meanwhile, friday already has a **precedent for always-on (no opt-out) injection** — `assemble_system_prompt()` unconditionally appends `GENERAL_AGENT_GUIDANCE` after the caller's prompt. Todo tracking is raised to the same standing, so that **it goes in unconditionally even if the caller does nothing**.

---

## 2. Goals / Non-Goals

### Goals
- `FridayAgent` **always auto-registers** the `TodoWrite` tool — no caller injection needed.
- todo usage **guidance (concept)** is **always auto-injected** into the system prompt — the same unconditional standing as `GENERAL_AGENT_GUIDANCE`.
- **Guarantee the integrity** of the built-in `TodoWrite`'s `state_effect` contract (which the loop depends on) — the caller cannot shadow it with the same name.

### Non-Goals (YAGNI)
- No opt-out flag. `FridayAgent.__init__` signature unchanged.
- The per-turn reminder (`render_todo_reminder`) is **already always-on** — no change.
- Built-in tools are not generalized into a generic plugin registry. Currently `TodoWrite` is the only built-in, but it is kept in accessor (`builtin_tools()`) form just to leave room for additions.
- The input boundary of caller todos (e.g. keeping them across user inputs in `run_agent.py`) · human-facing display remain **caller concerns** (the SDK is responsible only per step).

---

## 3. Key Design Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | **Injection at the engine boundary.** Merge built-in tools in `FridayAgent.__init__`, append the guidance in `assemble_system_prompt()`. | A single assembly point. The core loop (`run_one_turn`) stays uncoupled from any specific tool name — preserving the decoupling of the declarative `state_effect` design. |
| D2 | **Name clash = error (fail-fast).** If the caller passes a tool with the same name as a built-in, `ValueError`. | Anthropic rejects duplicate tool names, so dedupe is required anyway. "Silently prefer the caller" risks the caller shadowing the loop's todos contract with a broken `TodoWrite` variant, so integrity is kept via **explicit rejection**. |
| D3 | **Guidance lives in the core (prompts.py).** Put the `TODO_GUIDANCE` constant in `api/prompts.py` and have `assemble_system_prompt` append it. | Same philosophy as reminder rendering living in `core/loop.py` — the todo tool stays a stateless pure function, and the core owns the todo-aware text. `compact()` does not go through this path, so the guidance does not leak into the summary (same as the GENERAL block). |

---

## 4. Changes

### 4.1 Tool auto-registration — `friday_agent/tools/builtin/__init__.py`

Put the built-in accessor in the currently empty package `__init__`:

```python
from friday_agent.tools.base import Tool
from friday_agent.tools.builtin.todo_write import TodoWrite


def builtin_tools() -> list[Tool]:
    """Tools FridayAgent always registers, even without caller injection."""
    return [TodoWrite()]
```

### 4.2 Merge + clash check — `friday_agent/core/engine.py`

In `FridayAgent.__init__` (after the existing config type check), merge the caller tools and the built-ins, rejecting name clashes:

```python
from friday_agent.tools.builtin import builtin_tools
...
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

`step()` already builds `tool_schemas` from `self._tools`, so the schemas are reflected automatically (no further change).

### 4.3 Guidance auto-injection — `friday_agent/api/prompts.py`

Add a `TODO_GUIDANCE` constant next to `GENERAL_AGENT_GUIDANCE`, and have `assemble_system_prompt` filter out empty blocks and join the rest:

```python
TODO_GUIDANCE = """# Task tracking
You have a TodoWrite tool for tracking multi-step work. For any task with several
steps, call TodoWrite first to lay out the plan, then keep it updated as you go.
 - Always send the COMPLETE list each call; it replaces the previous one.
 - Keep exactly one item in_progress at a time; mark items completed the moment they are done.
 - Skip it for trivial single-step tasks.
The current list is surfaced to you each turn inside a <system-reminder>; it reflects
tracked state, not necessarily the user's latest instruction."""


def assemble_system_prompt(system_prompt: str) -> SystemPrompt:
    blocks = [b for b in (system_prompt, GENERAL_AGENT_GUIDANCE, TODO_GUIDANCE) if b]
    return SystemPrompt(text="\n\n".join(blocks))
```

### 4.4 Data Flow

```
FridayAgent.__init__(tools=caller_tools)
        └─ name clash check → self._tools = caller_tools + [TodoWrite()]

FridayAgent.step(state)
        └─ tool_schemas = [t.get_tool_schema() for t in self._tools]   ← TodoWrite always included
        └─ run_one_turn(...)
               └─ assemble_system_prompt(base) = base + GENERAL_AGENT_GUIDANCE + TODO_GUIDANCE
               └─ if state.todos: inject <system-reminder>   (unchanged)
```

---

## 5. Downstream Cleanup

### 5.1 `scripts/run_agent.py` (required)
Making it built-in turns the explicit wiring into **a duplicate and a source of errors**:
- `tools=[ExampleTool(), TodoWrite()]` → `tools=[ExampleTool()]` (TodoWrite is automatic). If left unfixed, the D2 check raises `ValueError`.
- Remove the `TodoWrite` import.
- Remove the hand-written todo sentences from `_SYSTEM_PROMPT` (the guidance is auto-injected).
- Update the banner text to reflect the built-in.
- **Keep**: keeping `todos` across user input boundaries, the `_render_todos` display (caller concerns).

### 5.2 Docs (SSOT sync)
- `docs/architecture/01-core-loop.md` §⑥ — add "TodoWrite tool + guidance auto-registration" to the always-inject note.
- `docs/architecture/02-tool-orchestration.md` — a note on built-in auto-registration (`ValueError` on name clash).
- `CLAUDE.md` — reflect built-in todo always-injection in one line on the public API/tool description line.

---

## 6. Tests (no API key required · fake provider)

- `builtin_tools()` returns a `TodoWrite` instance.
- `FridayAgent(provider, tools=[])` → `TodoWrite` is included in `self._tools` and in `step()`'s `tool_schemas`.
- `FridayAgent(provider, tools=[ExampleTool()])` → both ExampleTool + TodoWrite are registered.
- `FridayAgent(provider, tools=[TodoWrite()])` → `ValueError` (clash rejected). The message includes the tool name.
- Both `assemble_system_prompt("")` and `assemble_system_prompt("base")` include the `TODO_GUIDANCE` text, and also include `GENERAL_AGENT_GUIDANCE`.
- Integration: with no caller-injected tools, the fake provider emits a `TodoWrite` tool_use → the call is handled normally and the next `LoopState.todos` is updated.
- Regression: the existing 142 tests stay green (in particular, confirm `TODO_GUIDANCE` is not included on the `compact()` path).

---

## 7. Integrity · Scope Check

- **Charter compliance**: "system prompt assembly machinery" is in scope, and this change mirrors the existing `GENERAL_AGENT_GUIDANCE` always-inject mechanism as-is. It adds none of the excluded items (subagent delegation · streaming · model fallback, etc.).
- **Loop invariants preserved**: the core loop stays unaware of tool names (injection is at the engine boundary). The `tool_use↔tool_result` integrity and sole-state-writer invariants are unchanged.
- **Distributed resume unchanged**: tools · config are not serialized, so each container reconstructs them from the same code → built-in injection is deterministic. No change to `LoopState` serde.
