# Design Proposal: Integrating Todo / Task Tracking

- **Date**: 2026-06-01
- **Target**: `friday_agent` (distributed agent loop SDK)
- **Status**: design approved · not implemented · corrected against current code (`LoopState` sentinel) (this document is a proposal and contains no implementation plan or code)
- **Origin**: ports Claude Code's `todo_reminder` (per-turn automatic re-injection) mechanism to fit friday's pure-tool·distributed structure

---

## 1. Background & Motivation

In multi-step tasks, Claude Code **automatically re-injects the todo list into the context every turn** (`todo_reminder` attachment), so the model is always aware of current progress without calling the tool again. It is an anti-drift device that keeps the task list from being pushed out of attention in long conversations.

friday currently has **none** of this concept. Confirmed structural facts:

- The turn loop (`core/loop.py` › `run_one_turn`) has no point to insert context other than the system prompt. The only choke point is the single spot `messages_for_query = list(state_messages)`.
- `is_meta=True` messages are **removed from the API call** by `messages/normalize.py` › `normalize_for_api`. That is, friday's `is_meta` means "keep in the transcript but hide from the model" — the **exact opposite** of Claude Code's system-reminder ("visible to the model but not actual user input").
- Tools are **pure functions** of the form `call(args) -> ToolResult` and cannot access loop state (`tools/orchestrator.py` passes only `tool.call(block.input)`). Concurrency-safe tools run **in parallel** via `asyncio.gather`, so directly mutating shared state risks races.

Therefore, adding a "live todo reminder" requires designing both **a new injection path that is visible to the model but does not accumulate in `state`** and **a path for pure tools to update state**. This is the core of this work.

---

## 2. Goals / Non-Goals

### Goals
- The model is aware of the multi-step task todo list every turn without re-calling the tool (automatic reminder, faithful reproduction of Claude Code).
- Todo state is included in `LoopState` JSON serialization, so it is preserved identically across **per-turn distributed resume**.
- Does not break friday's **pure-tool design and sole-state-writer (the loop) invariant**.

### Non-Goals (YAGNI)
- Do not generalize into a general-purpose "context provider" channel. Limit to todos, but leave only the state-mutation effect protocol open for extension.
- Per-turn re-injection of CLAUDE.md / memory / RULES is out of scope. This is regarded as an area covered by the `system_prompt` input. (But see the design note in §8: friday's `system_prompt` is static, once per session, so it differs subtly from Claude Code's every-turn re-injection.)
- No "no todos" nudge·empty-state reminder (nothing is injected when the list is empty).
- No separate `TodoRead` tool (unnecessary, since the reminder always exposes the current list).

---

## 3. Key Design Decisions (Summary)

| # | Decision | Rationale |
|---|---|---|
| D1 | **Automatic reminder** approach (not tool-only) | Faithful reproduction of Claude Code `todo_reminder`; anti-drift in long conversations |
| D2 | Store todos in an **explicit `LoopState.todos` field** | single source of truth; preserved across distributed resume via `LoopState` serialization |
| D3 | Pure tools **return a declarative `ToolResult.state_effect`**, the loop applies it | The loop stays the sole state writer (zero races, deterministic); zero loop-to-tool-name coupling; fits the BYO-tool extension philosophy |
| D4 | The reminder is injected by **merging it into the last user turn** of the **API-view copy (`api_input_messages`)** | Visible to the API but does not accumulate in `state`/`LoopState`; role alternation preserved |

---

## 4. Design Details

### 4.1 Data Model & State — `core/state.py`

Add one JSON-native field to `LoopState`. Each todo is a `{"content": str, "status": "pending"|"in_progress"|"completed"}` dict; since input validation is handled at the boundary by TodoWrite's Pydantic schema (§4.3), state is kept as plain dicts to keep serialization simple.

```python
@dataclass
class LoopState:
    messages: list[Any]
    turn_count: int = 1
    todos: list[dict] = field(default_factory=list)          # NEW

    def to_dict(self):
        return {"messages": [m.to_dict() for m in self.messages],
                "turn_count": self.turn_count,
                "todos": self.todos}                          # NEW (already JSON-native)

    @classmethod
    def from_dict(cls, d):
        return cls(messages=[Message.from_dict(m) for m in d["messages"]],
                   turn_count=d.get("turn_count", 1),
                   todos=d.get("todos", []))                  # NEW, backward-compatible default
```

Since `LoopState.to_dict/from_dict` carries todos directly, **todos are transported as-is on distributed resume** — `LoopState` itself is both the "continue" sentinel and the transport unit. With `d.get("todos", [])`, old state dicts without todos do not break either (backward compatibility).

### 4.2 Mutation Protocol — `ToolResult.state_effect` (`tools/base.py`)

Instead of mutating directly, pure tools **return a declarative effect**, and the loop applies it. The loop remains the only state writer.

```python
@dataclass
class ToolResult:
    data: Any
    is_error: bool = False
    state_effect: dict | None = None     # NEW: e.g. {"todos": [...]}
```

Loop-side applier (small and tool-name-agnostic — it knows only the **state concept** `'todos'`, not the **tool name** `'TodoWrite'`):

```python
def apply_state_effects(todos: list[dict], effects: list[dict]) -> list[dict]:
    for eff in effects:
        if "todos" in eff:
            todos = eff["todos"]
    return todos
```

Other stateful effect keys will also be added to this applier in the future. With zero loop-tool coupling, integrators can write their own stateful tools without surgery on the loop.

### 4.3 TodoWrite Tool — `tools/builtin/todo_write.py` (new)

Full-list replacement approach (same as Claude Code — idempotent and distribution-friendly). The docstring is the description sent to the LLM, so write the "what·when" thoroughly.

```python
class TodoItem(BaseModel):
    content: str = Field(description="Imperative task description, e.g. 'Add the login endpoint'")
    status: Literal["pending", "in_progress", "completed"] = Field(
        description="pending = not started, in_progress = active now, completed = done")

class TodoWriteInput(BaseModel):
    todos: list[TodoItem] = Field(
        description="The COMPLETE updated list; it replaces the previous list entirely")

class TodoWrite(Tool):
    """Create and manage a structured task list for the current session. Use for
    multi-step work (3+ steps) to track progress and show the user a plan. Send the
    ENTIRE updated list every call — it replaces the previous one. Keep exactly one
    item in_progress at a time; mark items completed the moment they are done."""
    name = "TodoWrite"

    def input_schema(self):
        return TodoWriteInput

    def is_concurrency_safe(self, input_data: dict) -> bool:
        return False                                  # state mutation → sequential execution

    async def call(self, args: dict) -> ToolResult:
        todos = [t.model_dump() for t in TodoWriteInput(**args).todos]
        return ToolResult(data=f"Updated todo list ({len(todos)} items).",
                          state_effect={"todos": todos})
```

- With `is_concurrency_safe=False`, it is not batched in parallel with other tools (extra safety net). In practice the loop applies the effect, so races are impossible by construction.
- The "exactly one in_progress" rule is kept **soft** (guidance in the description + a warning string in `data` if needed). No hard rejection — so as not to break the turn.

### 4.4 Reminder Injection Channel — `core/loop.py`

The choke point is right after the spot where `state_messages` is copied into the API view (`list(state_messages)`), right before `normalize_for_api`. **Rebuild the last user turn as a new Message with a system-reminder block appended** and inject it only into the API-view-only copy (`api_input_messages`); **assembling the next LoopState uses the reminder-free `state_messages`** (prevents the leak in §4.5). Since no shared Message object is mutated, `state.messages`/`LoopState` stay unchanged, the reminder is visible to the API, and role alternation is preserved (no new consecutive user message is created; the block is only merged into the last user turn).

```python
api_input_messages = list(state_messages)                # API view only (next LoopState is assembled from state_messages)
if state.todos:                                          # no injection when empty (zero noise)
    api_input_messages = with_todo_reminder(api_input_messages, state.todos)
api_messages = normalize_for_api(api_input_messages)     # not is_meta → passed to the API
```

```python
def with_todo_reminder(messages, todos):
    block = ContentBlock(type="text", text=render_todo_reminder(todos))
    if messages and messages[-1].role == "user":
        last = messages[-1]
        merged = replace(last, content=[*last.content, block])   # new object (merged into the last user turn)
        return [*messages[:-1], merged]
    return [*messages, create_user_message([block])]             # defensive (never reached at turn start)
```

Render format:

```
<system-reminder>
Current todo list (update via TodoWrite as you progress; keep one item in_progress):
- [in_progress] Wire state_effect into the loop
- [pending] Add reminder injection
- [completed] Extend LoopState.todos
This reflects tracked state, not necessarily the user's latest instruction.
</system-reminder>
```

**Timing semantics**: the reminder reflects `state.todos` at the start of the turn (= committed state). If the model calls TodoWrite this turn, the effect is applied to the **next turn's** state and appears in the next reminder (this turn, it is confirmed immediately via `ToolResult.data`). These are the same semantics as Claude Code.

### 4.5 Effect Collection & Building the Next LoopState — `tools/orchestrator.py` + `core/loop.py`

A tool's `tool.call` is not called directly by `run_tools`; it runs **inside `_run_single_tool`**. Currently `_run_single_tool` builds the tool_result message from the `ToolResult` and then discards the result object (and thus `state_effect`). So to pull the effect out, `_run_single_tool` **returns the message and the effect together**, and `run_tools` accumulates them into the caller's sink in block order (message stream and effect collection kept separate; tool_result yield unchanged).

```python
# orchestrator._run_single_tool(block, tools) -> tuple[Message, dict | None]
result = await tool.call(block.input or {})
msg = create_tool_result_message(tool_use_id=block.id or "", result_text=str(result.data), is_error=result.is_error)
return msg, result.state_effect          # unknown-tool/exception paths return (error_msg, None)

# orchestrator.run_tools(..., effects_sink: list[dict] | None = None)
#   in each batch, receive (msg, effect): yield msg, append effect to sink if not None (block order preserved)
#   existing callers omit effects_sink → behavior unchanged
```

The loop gathers the sink to compute the next state's todos. **The next `LoopState` is assembled from the reminder-free `state_messages`** (preserving the §6 non-persistence invariant — not from `api_input_messages`):

```python
effects: list[dict] = []
async for result_msg in run_tools(tool_use_blocks, tools, max_concurrency=..., effects_sink=effects):
    tool_results.append(result_msg)
    yield result_msg

next_todos = apply_state_effects(state.todos, effects)   # no effect → carried forward as-is
yield LoopState(
    messages=[*state_messages, *assistant_messages, *tool_results],   # clean original — reminder not included
    turn_count=next_turn_count,
    todos=next_todos)                                    # NEW
```

On a turn with no effect, `state.todos` is carried forward unchanged to the next turn.

---

## 5. Data Flow of a Single Turn

```
state(todos=[A:pending])
  → api_input_messages = state.messages + <reminder: A pending>     (visible to API, not in state)
  → provider.complete(...)
  → assistant: tool_use TodoWrite(todos=[A:in_progress, B:pending])
  → run_tools:
        ToolResult(data="Updated (2)", state_effect={todos:[A:in_progress, B:pending]})
        · yield tool_result message (immediate confirmation to the model)
        · effects_sink += that effect
  → LoopState(todos = [A:in_progress, B:pending])   ← next turn's reminder reflects this
```

---

## 6. Invariant Preservation & Distributed Safety

- **tool_use ↔ tool_result integrity**: the reminder merges into an existing user turn (never wedged between the two), and applying effects does not change message structure → integrity preserved.
- **Role alternation**: no new consecutive user message is created; a block is only added to the last user turn → API-safe.
- **Distributed resume**: todos are serialized via `LoopState`, and the reminder is non-persistent·derived from todos every turn → resuming in container B regenerates the same reminder (deterministic).
- **Zero races**: tools never touch state and only return effects; the loop is the sole writer → independent of parallel batching.
- **Backward compatibility·incremental adoption**: old checkpoints are fine thanks to `from_dict`'s default `todos=[]`. If TodoWrite is not registered in `tools`, todos stay empty and the reminder·effect hooks all become harmless no-ops.

---

## 7. Test Strategy

- **Unit**
  - `LoopState` serialization round-trip: with todos, and loading an old dict without a todos key.
  - `TodoWrite.call`: returns the correct `state_effect`, schema validation, `is_concurrency_safe=False`.
  - `apply_state_effects`: effect present → replace, no effect → carry-forward.
  - `render_todo_reminder`: no injection when empty, renders a system-reminder when there are items.
  - `with_todo_reminder`: merges into the last user turn, `state.messages` unchanged, no consecutive user messages; reminder not included in the next `LoopState.messages` (non-persistent).
- **Integration (fake provider)**
  - Model calls TodoWrite → next turn's `api_messages` includes the updated reminder, todos present in `LoopState` serialization.
- **Distributed**
  - After a TodoWrite turn, `LoopState` goes through `to_dict → json → from_dict` ("container B") → todos preserved → identical reminder regenerated.

---

## 8. Change Surface (touch map)

| File | Change | Nature |
|---|---|---|
| `core/state.py` | `LoopState.todos` field + `to_dict`/`from_dict` | State contract extension |
| `tools/base.py` | `ToolResult.state_effect` field | Mutation protocol |
| `tools/orchestrator.py` | `_run_single_tool` returns `(Message, state_effect)` + accumulated via `run_tools(effects_sink=)` | Wiring |
| `core/loop.py` | ① reminder injection via `render_todo_reminder`/`with_todo_reminder` (API-view-only `api_input_messages`) ② build the next `LoopState` via `apply_state_effects` (from the clean `state_messages`) | Loop solely owns todos state read/apply |
| `core/engine.py` | `compact()` carries forward `todos=state.todos` | 1 line |
| `tools/builtin/todo_write.py` | **New** TodoWrite tool (declares state effect only; rendering owned by core) | New |
| `tools/builtin/__init__.py` | No change — imported directly by module path per the existing pattern (same as `ExampleTool`) | — |
| `tests/` | Unit + integration (distributed resume) | Verification |
| `docs/architecture/` | Reflected in 01-core-loop (reminder), 02-tool-orchestration (state_effect), 07-data-models (todos) | Doc sync |

> `core/engine.py` **changes only 1 line in `compact()`** — when assembling the new `LoopState` after summarization, it carries forward `todos=state.todos`. Compaction happens in long conversations, which is exactly when todo anti-drift is needed most, so todos must not be dropped (the summary is prose and todos are structured state, so they are preserved separately; treated the same as `turn_count`). The reminder·effect application itself is all handled inside `run_one_turn` via `state.todos`.

> **Design note**: friday's `system_prompt` is assembled statically once per session (`api/prompts.py` › `assemble_system_prompt`), so there is a subtle fidelity gap versus Claude Code re-injecting environment context every turn. This proposal's reminder channel (§4.4) introduces that "every-turn re-injection" pattern for the first time, limited to todos, and leaves the same mechanism in a form extensible to other per-turn context later (out of scope).
