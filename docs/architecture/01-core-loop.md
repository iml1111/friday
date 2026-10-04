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
| `friday_agent/core/loop.py` | Single-turn execution · stop_reason branching · deferred calls · pending-call guard · resume | `run_one_turn()`, `pending_tool_uses()`, `resume()` |
| `friday_agent/core/engine.py` | External entry point, direct provider injection, memory tool registration · section injection | `FridayAgent.step()`, `FridayAgent.compact()` |
| `friday_agent/core/state.py` | Loop state · termination types + JSON serde | `Terminal`, `Suspended`, `LoopState` (`to_dict`/`from_dict`), `PendingToolUseError` |

---

## ③ Core Behavior — `run_one_turn()` Turn Lifecycle

`run_one_turn()` is an `AsyncGenerator` defined at `friday_agent/core/loop.py:162`. It yields every `Message` produced during the turn, then yields exactly **1 sentinel** (`LoopState`, `Suspended` or `Terminal`) at the end and finishes.

### Execution Order

```
0. pending_tool_uses(state) non-empty → raise PendingToolUseError (no request)
1. api_input_messages = list(state.messages)
      └─ inject <system-reminder> via with_turn_reminders() (API view only · non-persistent):
         in order: [todo reminder (when state.todos is set)] + turn_reminders param (engine passes memory index, then step()'s turn_sections)
      └─ normalize_for_api(api_input_messages) → provider.complete()   ← LLM call

2. response → _to_assistant_message()     ← converted to internal Message, then yielded

3. if there are tool_use blocks
      ├─ deferred calls (Tool.is_deferred → True) are held back
      └─ run_tools(the rest, effects_sink=effects) ← parallel tool execution + state_effect collection
            └─ yield tool_result message (each result)

4. end of turn: yield 1 sentinel (next_todos = apply_state_effects(state.todos, effects))
      LoopState              ─ loop continues (clean state.messages + next_todos)
      Suspended              ─ paused on deferred calls: .state (to persist) + .pending (tool_use blocks)
      Terminal               ─ loop terminates; .state = the state to persist
```

### Termination / Transition Branch Table

`run_one_turn()` actually emits 2 reasons.

| Condition | Result |
|---|---|
| State has unanswered `tool_use` (pending) | **raises `PendingToolUseError`** before any request — `step()` and `compact()` alike |
| No tool_use (non-tool stop such as end_turn) | `Terminal(reason="completed", state=…)` — `state` = input + assistant message, `turn_count+1` |
| `LLMError` (excluding overflow) | `Terminal(reason="model_error", error=..., state=…)` — `state` = the input state; retry with `step(terminal.state)` |
| `ContextOverflowError` | **raised to the caller** (not a Terminal) — the caller trims, `engine.compact()`s and retries |
| Tool execution complete | `LoopState(..., turn_count+1)` |
| tool_use includes deferred calls (`Tool.is_deferred`) | the other calls run first, then `Suspended(state, pending)` — `state.turn_count+1` |

### Final State on `Terminal`

Every `Terminal` carries `state` — the state to persist. `completed`: the input state plus this turn's assistant message (`turn_count+1`, `todos` unchanged). `model_error`: the input state itself — the provider call failed before any assistant message existed, so there is nothing to add and no `tool_use` to pair; `step(terminal.state)` repeats the call. No extra `LoopState` is yielded before a `Terminal` (`LoopState` means "continue").

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
    memory=None,           # MemoryStore — if unset, memory subsystem not mounted (opt-in); the store is the tool surface
    compact_instructions="",  # domain requirements to insert into the compact() summary prompt; empty string leaves the prompt unchanged
)
```

If `config` is not of type `provider.config_type`, `ValueError` is raised immediately.

The context injection surface is intentionally simple: static content is passed by the caller as a single `system_prompt` string (multiple sections are combined on the caller side with `"\n\n".join(...)`). Per-turn content has exactly one hook, the `turn_sections` argument of `step(state, turn_sections=[...])`: a list of strings the caller computes for that call only, so each call can pass different sections or none. Empty strings are dropped, and the SDK wraps the rest in `<system-reminder>` (the prefix the Anthropic adapter's breakpoint skip detects) and joins them onto the trailing user message after the todo reminder and the memory index — the `turn_reminders` path of `run_one_turn`.

`compact()`'s summarization call carries `system_prompt` only as the shared cached prefix — the compaction prompt after it governs the reply. What the summary must preserve is specified by a separate string, `compact_instructions` (details: [04-context-compaction](04-context-compaction.md#domain-instruction-injection-slot-opt-in)).

### `engine.step(state, turn_sections=None) -> AsyncGenerator[Message | LoopState | Suspended | Terminal, None]`

An **async generator** that executes one turn. It immediately yields each `Message` produced while the turn progresses (assistant response, each tool_result), then yields exactly **1 sentinel** (`LoopState`, `Suspended` or `Terminal`) at the end and finishes.

```python
async for item in engine.step(state):
    if isinstance(item, (LoopState, Suspended, Terminal)):
        outcome = item        # LoopState → next turn (use outcome as state as-is)
                              # Suspended → persist outcome.state; run outcome.pending elsewhere
                              # Terminal  → loop terminates (outcome.state = state to keep)
    else:
        render(item)          # Message: assistant response or tool_result — consumable on arrival
```

`ContextOverflowError` is not consumed; it propagates to the caller as-is.

### `await engine.compact(state) -> LoopState`

Reduces all of `state.messages` to a single summary. `turn_count` is preserved.

The summary call sends `step()`'s exact system prompt, tools and config, so the provider serves the history from its prompt cache (see [04-context-compaction](04-context-compaction.md)). It is therefore `step()`'s request plus the compaction prompt: compact proactively, or trim an overflowed state first.

`ContextOverflowError` recovery flow:

```
step(state) → ContextOverflowError raised
    └─ caller trims state (its own policy, e.g. drop the oldest turns)
          └─ compact(trimmed) → reduced LoopState
                └─ retry step(reduced state)
```

See [04-context-compaction](04-context-compaction.md) for details.

### Pending Calls — `pending_tool_uses(state)` / `PendingToolUseError`

`pending_tool_uses(state)` (`core/loop.py`) returns the `tool_use` blocks of the last assistant message that have no `tool_result` after it. It is a pure function of history, so it gives the same answer for a deserialized state. While it is non-empty, `step()` and `compact()` raise `PendingToolUseError(tool_use_ids)` (a `ValueError`, not an `LLMError`) before doing anything — even when a user message was appended after the unanswered calls. Previously such a state reached the API and came back as a 400 `model_error`.

### Deferred Tools — `Suspended` → `resume(state, results)`

A tool whose `is_deferred(input)` returns `True` is not executed by `step()`. The other calls in the response run as usual, then the turn ends with `Suspended(state, pending)`. The caller persists `state` (an ordinary `LoopState`), hands `pending` (the deferred `tool_use` blocks) to whoever produces the results — a person, another service, a job queue — and finishes. When results arrive, possibly in another process:

```python
state = LoopState.from_dict(load())
pending_tool_uses(state)                         # the calls still waiting
state = resume(state, {tool_use_id: ToolResult(data="approved")})
async for item in engine.step(state): ...        # the next turn, as usual
```

`resume()` is a pure function: no provider, no tools, no model call. It converts each result with `to_tool_result_message()` (the same conversion `run_tools` uses), inserts it in `tool_use` order ahead of any other trailing message, applies `state_effect`s, and keeps `turn_count`. Partial results are allowed (the rest stay pending); an id that is not pending raises `ValueError`. To close a call that will never finish (cancel, timeout, superseding instruction), pass an `is_error=True` result, append the new user message, and call `step()`.

### State Types

| Type | Defined At | Role |
|---|---|---|
| `LoopState(messages, turn_count=1, todos=[])` | `core/state.py:41` | Serializable loop transport unit + turn-boundary "continue" resume sentinel |
| `Terminal(reason, error=None, state=None)` | `core/state.py:20` | Loop termination sentinel; `state` is always set by the loop |
| `Suspended(state, pending)` | `core/state.py` | "Paused on deferred tool calls" sentinel; persist `state`, `pending` is recomputable via `pending_tool_uses()` |

---

## ⑤ Dependencies

| Dependency Module | Purpose |
|---|---|
| `friday_agent/messages/normalize.py` | `normalize_for_api()` — internal Message → API payload conversion |
| `friday_agent/tools/orchestrator.py` | `run_tools()` — parallel tool execution |
| `friday_agent/api/provider.py` | `LLMProvider`, `LLMError`, `ContextOverflowError`, response block types |
| `friday_agent/api/prompts.py` | `format_compact_prompt()`, `format_compact_summary_message()` — prompt and summary message texts for `engine.compact()` |
| `friday_agent/api/prompts.py` | `assemble_system_prompt()` — system prompt assembly + general behavior block injection |
| `friday_agent/memory/store.py` | `MEMORY_INSTRUCTIONS` (static instructions, for system) + `build_memory_reminder()` (per-turn index reminder); default `FileMemoryStore`/`MemoryStore` types |

---

## ⑥ Maintenance Notes

- **Context window management is the caller's responsibility.** `step()` sends `state.messages` to the API as-is. When the token budget is exceeded it throws `ContextOverflowError`, so the caller must reduce the state — trim, then `engine.compact(state)` (the summary call re-sends `step()`'s prefix) — and retry.
- **General behavior block auto-injection.** `run_one_turn()` **always** injects `GENERAL_AGENT_GUIDANCE` (prompt-injection flagging · meaning of `<system-reminder>` · reversibility of actions · conciseness, etc.) **before** the caller's `system_prompt` when sending (`assemble_system_prompt()`). Order is general→specific — the domain prompt comes last so its rules override the general guidance via recency. There is no opt-out flag. The compaction summary call (`engine.compact()`) sends the identical system prompt on purpose, to share the cached prefix.
- **TodoWrite tool · guidance auto-injection (built-in).** `FridayAgent` always merges the tools from `builtin_tools()` (`tools/builtin/__init__.py`) into the caller's tools, and `assemble_system_prompt()` always appends `TODO_GUIDANCE` (no opt-out). If the caller injects a tool with the same name as a built-in, `FridayAgent.__init__` rejects it with `ValueError`. The compaction summary call sends it too, with the rest of the shared prefix.
- **Memory prompt injection (opt-in, static/dynamic split).** Only when a `memory=` store is mounted: `engine.step()` places the static instructions `MEMORY_INSTRUCTIONS` before the base system prompt (general→specific, byte-stable within a session), and renders the live index every turn via `build_memory_reminder(self._memory)`, carrying it as a turn-local reminder (`turn_reminders` path) on `messages[-1]` only. With `memory=None` (default), this entire path is skipped. `compact()` never renders the live index, so it does not leak into the summary (only the static `MEMORY_INSTRUCTIONS` rides along in the shared prefix). `MemoryStore` is not serialized into `LoopState` (container-local re-injection), so distributed-resume serde is unchanged. From the prompt caching (always-on) perspective: even when `memory_save`/`delete` changes the index, the system prefix · conversation history caches survive — only the reminder block outside the breakpoints changes (putting the index in system would invalidate the whole conversation cache on a single save). See [08-memory](08-memory.md) for details.
- **`tool_use↔tool_result` pair preservation.** `run_tools()` emits exactly one `tool_result` per executed `tool_use` (unknown tools and exceptions become error results), and the only error path (`LLMError` from the provider call) fires before an assistant message exists — so no unpaired `tool_use` is ever persisted. If this invariant breaks, the next API call fails immediately. See [06-invariants](06-invariants.md) for details.
- **Loop state is updated only at clean turn boundaries.** `LoopState` is yielded only after all tool results are collected, so no intermediate state is lost on serialization · resume.
- **serde does not serialize provider · config.** `LoopState.to_dict()` / `LoopState.from_dict()` round-trip only messages + turn_count + todos. provider · config are treated as container-local objects and re-injected on resume.
- **Per-turn reminders are non-persistent.** Every turn, `run_one_turn()` uses `with_turn_reminders()` to merge `<system-reminder>` blocks (todo reminder + caller-provided `turn_reminders`) into the last user turn of an **API-view-only copy (`api_input_messages`)** and sends it. The next `LoopState` is assembled from the reminder-free `state.messages`, so reminders do not accumulate in state and are deterministically regenerated from sources such as `todos` on distributed resume. `engine.compact()` also carries `todos` forward (the summary is prose, todos are structured state). Cache invariant: all per-turn varying text is carried only on `messages[-1]` — everything up to `messages[-2]` must be byte-stable for the provider's rolling breakpoint to keep hitting. `step()`'s `turn_sections` follow the same path (after the memory index), so they never reach `LoopState`, the cached prefix, or `compact()`.

---

## ⑦ Design Rationale (Why)

**Why single-turn step-only?**  
By not placing a while-true driver inside the library, the same `FridayAgent.step()` can be reused across diverse server-side execution contexts such as distributed queues · serverless · server handlers.

**Why emit `LoopState` as-is for stateless resume?**  
Emitting a serializable `LoopState` at the turn boundary means that even after a process restart or container move, resuming is just passing the same `LoopState` to `step()`. The types' `to_dict()`/`from_dict()` methods handle the JSON round-trip, and provider · config are excluded from serialization (container-local).

**Why is `resume()` a pure function, not an engine method?**  
Attaching a result needs neither the provider (credentials) nor the tools, so the process that receives an external result — a webhook, a queue worker — can attach it and persist the state without building an agent. `step()` remains the single entry point that runs a turn.
