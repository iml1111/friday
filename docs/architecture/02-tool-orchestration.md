# 02 — Tool Orchestration

> **Related docs**: [00-overview](00-overview.md) · [01-core-loop](01-core-loop.md) · [06-invariants](06-invariants.md)

---

## ① Purpose

The layer that takes a list of `tool_use` blocks and performs **partitioning → parallel/sequential execution → returning results in the blocks' original order**.

Core contract:
1. Always preserves `tool_use`↔`tool_result` pairing integrity — it never breaks, whether in parallel execution or on error paths.
2. Results are yielded **in block input order**, leveraging `asyncio.gather`'s argument-order guarantee.
3. Unknown tools · exceptions do not abort the whole batch; an error `tool_result` is generated for the affected block.

---

## ② Owned Files

| Path | Responsibility | Key Symbols |
|---|---|---|
| `friday_agent/tools/orchestrator.py` | Partitioning · parallel/sequential execution · order preservation | `partition_tool_calls()`, `run_tools()`, `to_tool_result_message()`, `Batch` |
| `friday_agent/tools/base.py` | Tool interface · result type | `Tool`, `ToolResult` |
| `friday_agent/tools/builtin/example_tool.py` | Demo tool (authoring pattern) | `ExampleTool` |

---

## ③ Core Behavior / Data Flow

### Partitioning (`partition_tool_calls`)

`orchestrator.py:67` — takes `blocks: list[ContentBlock]` and returns `list[Batch]`.

```
[RO, RO, RO, MUT, RO, RO]
  ↓
[Batch(is_concurrency_safe=True, 3 blocks),
 Batch(is_concurrency_safe=False, 1 block),
 Batch(is_concurrency_safe=True, 2 blocks)]
```

**Merge rule**: consecutive concurrency-safe blocks are merged into one parallel batch. A non-safe block always becomes its own batch.

**Conservative fallback** (`orchestrator.py:40–64`): treated as non-safe if any of the following applies.
- Tool not found (unknown tool)
- Input is `None`
- Pydantic schema validation fails
- `is_concurrency_safe()` itself raises an exception

**concurrency-safe determination**: whether a block goes into a parallel batch is **decided solely by `is_concurrency_safe()`**. `_is_concurrency_safe()` (`orchestrator.py:40–64`) calls only `tool.is_concurrency_safe()` after schema validation passes (`orchestrator.py:62`). That is, returning `is_concurrency_safe() → True` is all it takes to become eligible for parallel execution. The default is `False`, so without an explicit override the tool runs sequentially.

---

### Execution Path — `run_tools`

`orchestrator.py:144` / `core/loop.py:40,261` — the only execution path that `run_one_turn()` calls directly.

```python
# core/loop.py
from friday_agent.tools.orchestrator import run_tools

# core/loop.py — collect each tool's state_effect via effects_sink (message yield order unchanged)
async for result_msg in run_tools(tool_use_blocks, tools, max_concurrency=max_concurrency, effects_sink=effects):
```

Behavior:
1. Builds the batch list via `partition_tool_calls(blocks, tools)`.
2. Parallel batch (`is_concurrency_safe=True` AND `len > 1`): limits concurrency with `asyncio.Semaphore(max_concurrency)` and runs via `asyncio.gather`. Results are yielded in block order.
3. Otherwise (sequential batch or a single block): runs blocks one at a time, in order.
4. Unknown tool · exception → generates an error `tool_result`, batch continues.
5. If `effects_sink` (optional argument) is given, each tool's `ToolResult.state_effect` (when not None) is accumulated into the sink **in block order**. The message stream is unchanged, and `_run_single_tool` returns a `(Message, state_effect)` tuple. The loop gathers this sink to compute the next `LoopState.todos` (sole state writer = the loop).

---

### Data Flow Summary

```
run_one_turn()
    │
    ├─ extract tool_use blocks
    │
    └─ run_tools(blocks, tools, max_concurrency)  ← loop wiring path
            │
            ├─ partition_tool_calls()
            │       └─ [Batch(safe, N), Batch(non-safe, 1), ...]
            │
            ├─ parallel Batch: asyncio.gather + Semaphore → yield in block order
            └─ sequential Batch: yield blocks one at a time, in order
                    │
                    each block → _run_single_tool() → to_tool_result_message() → tool_result Message
                                     └─ on error: error tool_result (batch not aborted)
```

---

## ④ Public API / Extension Points

### Writing a Custom Tool (BYO Tool)

All it takes is subclassing `Tool` and implementing `input_schema()` and `call()`.

```python
from pydantic import BaseModel, Field
from friday_agent.tools.base import Tool, ToolResult


class WeatherInput(BaseModel):
    city: str = Field(description="Name of the city to look up the weather for")


class WeatherTool(Tool):
    """Returns the current weather for the given city."""

    name = "get_weather"

    def input_schema(self) -> type[BaseModel]:
        return WeatherInput

    def is_concurrency_safe(self, input: dict) -> bool:
        return True  # allow parallel execution

    async def call(self, args: dict) -> ToolResult:
        parsed = WeatherInput(**args)
        # replace with a real external API call
        return ToolResult(data=f"{parsed.city}: sunny, 22°C")
```

To be eligible for a parallel batch, `is_concurrency_safe()` just needs to return `True` — partitioning (`_is_concurrency_safe`, `orchestrator.py:40–64`) consults only this single predicate.

---

### `Tool` Methods — Conservative Defaults

`tools/base.py:139`

| Method | Return Type | Default | Description |
|---|---|---|---|
| `input_schema()` | `type[BaseModel]` | _(abstract)_ | Input schema. **Must implement** |
| `call(args)` | `ToolResult` | _(abstract)_ | Execution logic. **Must implement** |
| `is_concurrency_safe(input)` | `bool` | `False` | Whether parallel execution is allowed |

---

### `ToolResult`

`tools/base.py:17`

```python
ToolResult(
    data,                   # execution result (string or structured data)
    is_error=False,
    state_effect=None,      # declarative state mutation (e.g. {"todos": [...]}); applied solely by the loop
    image=None,             # {"media_type": "image/png", "data": "<base64>"} — sent as an image block next to the text
)
```

`to_tool_result_message(tool_use_id, result)` is the single `ToolResult` → `tool_result` message conversion (data → text, `is_error` → `<tool_use_error>` wrapping, `image` → block array). It is shared by `run_tools` and `resume()`.

---

### `get_tool_schema()`

`tools/base.py` — runs the Pydantic v2 schema through the wire-diet pipeline and returns it in the form passed to the API. Since the schema resides in the prefix of every call, bytes carrying zero information for the model are removed (validation is done by the Pydantic model — this schema is purely "documentation shown to the model", so semantics are unchanged):

1. **`_inline_defs`** — inlines and removes `$defs`. Ensures nested models · enums (e.g. `TodoItem.status`) are exposed to the model as-is without dangling `$ref`s — otherwise only `$ref` remains and the model cannot see the enum constraint.
2. **`_strip_titles`** — recursively removes pydantic's auto-generated cosmetic `title` (pure duplication, since the property name is already in the schema). Actual properties named `title` (dict values) are preserved.
3. **`_slim_wire_schema`** — collapses the triple ceremony of `Optional[X] = None` (`anyOf: [X, {type: null}]` + `default: null`) into X and removes the null default ("not required" is already conveyed by absence from `required`). Non-null unions · meaningful defaults are preserved. Nested descriptions are dedented.
4. **description precedence** — the `Tool.description` class attribute is the model-facing description; if unset, falls back to the docstring (backward compatibility). This is the separation point that keeps developer-facing implementation notes in the docstring from leaking onto the wire. Source indentation is removed via `_dedent_text`.

```python
{
    "name": self.name,
    "description": _dedent_text(self.description or docstring),
    "input_schema": { ... }  # recursive title removal + Optional collapsing; $defs inlined then removed
}
```

---

## ⑤ Dependencies

| Direction | Module | Reason |
|---|---|---|
| Uses | `messages/types.py` | `ContentBlock`, `create_tool_result_message` |
| Uses | `pydantic` | Input schema validation (`model_validate`, `model_json_schema`) |
| Called by | `core/loop.py` | imports · calls `run_tools` (`loop.py:40,261`) |

---

## ⑥ Maintenance Notes

**Result order invariant** — even with parallel execution, `run_tools` returns results **in tool_use block input order** (see [06-invariants](06-invariants.md)). `asyncio.gather` guarantees argument order, so adding reordering code is prohibited.

**Pairing on error paths** — `run_tools()` emits exactly one `tool_result` per executed `tool_use`: unknown tools and exceptions become error results, never a gap. The loop's only error path (`LLMError` from the provider call) fires before an assistant message exists, so no `tool_use` is ever left without its `tool_result` (see [06-invariants](06-invariants.md)).


**`is_concurrency_safe` conservative default** — when you write a new tool, the default is `False`, so parallel batches are not formed unintentionally. If you want parallel execution, you must explicitly override `is_concurrency_safe()` to return `True` — partitioning consults only this single predicate.

**Built-in tool auto-registration** — `FridayAgent` always merges the tools returned by `builtin_tools()` (`friday_agent/tools/builtin/__init__.py`) (currently `TodoWrite`) after the caller's tools. If the caller passes a tool with a clashing name, `__init__` rejects it with `ValueError` (the LLM API rejects duplicate tool names, so integrity is kept via explicit rejection rather than silent dedupe). Injection happens only at the engine boundary, so the orchestrator · loop remain unaware of tool names. In addition, when a store is mounted via `memory=` (opt-in), that `MemoryStore`'s `tools()` (default `memory_save`/`memory_read`/`memory_delete`) are also registered, and if a caller tool's name clashes with a built-in or memory tool name, `__init__` rejects it with `ValueError` (uniqueness check across all tool names).

---

## ⑦ Design Rationale (Why)

**Partitioning strategy** — grouping only read-only tools in parallel and isolating mutating tools is a safe default proven in the original Friday design. The conservative fallback that treats unknown tools or schema validation failures as non-safe prevents side effects from parallel execution during the intermediate state while new tools are being deployed.
