# 01. Core Loop

The subsystem responsible for the agent loop's **single-turn execution** and **termination / recovery / resume**.

---

## ① Purpose

The `core/` subsystem has two responsibilities.

1. **Single-turn execution** — completes one cycle of LLM call → response receipt → tool_use execution → result feedback (`run_one_turn()`).
2. **Termination / recovery / resume** — preserves `tool_use↔tool_result` integrity on every path (normal completion · error · context overflow), and emits a serializable `LoopState` at every turn boundary to support stateless distributed resume.

There is no while-true driver. The caller drives the loop directly by calling `step()` repeatedly.

---

## ② Owned Files

| Path | Responsibility | Key Symbols |
|---|---|---|
| `friday_agent/core/loop.py` | Single-turn execution · stop_reason branching · backfill | `run_one_turn()`, `yield_missing_tool_result_blocks()` |
| `friday_agent/core/engine.py` | External entry point, direct provider injection | `FridayAgent.step()`, `FridayAgent.compact()` |
| `friday_agent/core/state.py` | Loop state · termination types + JSON serde | `Terminal`, `LoopState` (`to_dict`/`from_dict`) |

---

## ③ Core Behavior — `run_one_turn()` Turn Lifecycle

`run_one_turn()` is an `AsyncGenerator` defined at `friday_agent/core/loop.py:114`. It yields every `Message` produced during the turn, then yields exactly **1 sentinel** (`Terminal` or `LoopState`) at the end and finishes.

### Execution Order

```
1. normalize_for_api(state.messages)
      └─ provider.complete()       ← LLM call

2. response → _to_assistant_message()     ← converted to internal Message, then yielded

3. if there are tool_use blocks
      └─ run_tools()                     ← parallel tool execution
            └─ yield tool_result message (each result)

4. end of turn: yield 1 sentinel
      LoopState              ─ loop continues
      Terminal               ─ loop terminates
```

### Termination / Transition Branch Table

`run_one_turn()` actually emits 2 reasons.

| Condition | Result |
|---|---|
| No tool_use (non-tool stop such as end_turn) | `Terminal(reason="completed")` |
| `LLMError` (excluding overflow) | `Terminal(reason="model_error", error=...)` + backfill |
| `ContextOverflowError` | **raised to the caller** (not a Terminal) — retry after `engine.compact()` |
| Tool execution complete | `LoopState(..., turn_count+1)` |

> **Drift warning**: the `Terminal` docstring in `friday_agent/core/state.py:19` also lists `blocking_limit` / `image_error` / `hook_stopped`, but the only reasons `run_one_turn()` actually emits are the 2 above (`completed` / `model_error`).

### Backfill (`yield_missing_tool_result_blocks`)

When a turn is aborted by `LLMError`, `friday_agent/core/loop.py:75` › `yield_missing_tool_result_blocks()` generates synthetic error `tool_result`s for tool_use blocks that have not yet received results, restoring integrity. See [06-invariants](06-invariants.md) for details.

---

## ④ Public API / Extension Points

### Constructing `FridayAgent`

```python
FridayAgent(
    provider,              # LLMProvider — required
    tools=None,            # list[Tool]
    system_prompt="",
    config=None,           # if unset, provider.config_type() defaults
    max_concurrency=10,
)
```

If `config` is not of type `provider.config_type`, `ValueError` is raised immediately (`friday_agent/core/engine.py:71`).

### `engine.step(state) -> AsyncGenerator[Message | LoopState | Terminal, None]`

An **async generator** that executes one turn. It immediately yields each `Message` produced while the turn progresses (assistant response, each tool_result), then yields exactly **1 sentinel** (`LoopState` or `Terminal`) at the end and finishes.

```python
async for item in engine.step(state):
    if isinstance(item, (LoopState, Terminal)):
        outcome = item        # LoopState → next turn (use outcome as state as-is)
                              # Terminal  → loop terminates
    else:
        render(item)          # Message: assistant response or tool_result — consumable on arrival
```

`ContextOverflowError` is not consumed; it propagates to the caller as-is.

### `await engine.compact(state) -> LoopState`

Reduces all of `state.messages` to a single summary. `turn_count` is preserved. `ContextOverflowError` recovery flow:

```
step(state) → ContextOverflowError raised
    └─ compact(state) → reduced LoopState
          └─ retry step(reduced state)
```

See [04-context-compaction](04-context-compaction.md) for details.

### State Types

| Type | Defined At | Role |
|---|---|---|
| `LoopState(messages, turn_count=1)` | `core/state.py:39` | Serializable loop transport unit + turn-boundary "continue" resume sentinel |
| `Terminal(reason, error=None)` | `core/state.py:19` | Loop termination sentinel |

---

## ⑤ Dependencies

| Dependency Module | Purpose |
|---|---|
| `friday_agent/messages/normalize.py` | `normalize_for_api()` — internal Message → API payload conversion |
| `friday_agent/tools/orchestrator.py` | `run_tools()` — parallel tool execution |
| `friday_agent/api/provider.py` | `LLMProvider`, `LLMError`, `ContextOverflowError`, response block types |
| `friday_agent/context/compact.py` | `compact_conversation()`, `create_compact_summary_message()` — implementation of `engine.compact()` |
| `friday_agent/api/prompts.py` | `assemble_system_prompt()` — system prompt assembly + general behavior block injection |

---

## ⑥ Maintenance Notes

- **Context window management is the caller's responsibility.** `step()` sends `state.messages` to the API as-is. When the token budget is exceeded it throws `ContextOverflowError`, so the caller must reduce via `engine.compact(state)` and retry.
- **General behavior block auto-injection.** `run_one_turn()` **always** appends `GENERAL_AGENT_GUIDANCE` (prompt-injection flagging · meaning of `<system-reminder>` · hooks · reversibility of actions · conciseness, etc.) after the caller's `system_prompt` when sending (`loop.py:166` › `assemble_system_prompt()`). There is no opt-out flag. The compaction summary call (`engine.compact()`) does not go through this path, so the general block does not leak into the summary.
- **`tool_use↔tool_result` pair preservation.** On the `LLMError` path, backfill (`yield_missing_tool_result_blocks`) kicks in to prevent LLM API rejection. If this invariant breaks, the next API call fails immediately. See [06-invariants](06-invariants.md) for details.
- **Loop state is updated only at clean turn boundaries.** `LoopState` is yielded only after all tool results are collected, so no intermediate state is lost on serialization · resume.
- **serde does not serialize provider · config.** `LoopState.to_dict()` / `LoopState.from_dict()` round-trip only messages + turn_count. provider · config are treated as container-local objects and re-injected on resume.

---

## ⑦ Design Rationale (Why)

**Why single-turn step-only?**  
By not placing a while-true driver inside the library, the same `FridayAgent.step()` can be reused across diverse server-side execution contexts such as distributed queues · serverless · server handlers.

**Why emit `LoopState` as-is for stateless resume?**  
Emitting a serializable `LoopState` at the turn boundary means that even after a process restart or container move, resuming is just passing the same `LoopState` to `step()`. The types' `to_dict()`/`from_dict()` methods handle the JSON round-trip, and provider · config are excluded from serialization (container-local).
