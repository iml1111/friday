# Design Spec: Persistent Memory — Always-On Built-in + Replaceable

- **Date**: 2026-06-01
- **Target**: `friday_agent` (distributed agent loop SDK)
- **Status**: design approved · not implemented
- **Predecessor/source**: [`docs/proposals/2026-06-01-friday-memory-design.md`](../../proposals/2026-06-01-friday-memory-design.md) (proposal). This spec inherits that proposal as the authoritative baseline, but reverses **D6 (no engine changes) → engine-integrated always-on** (§2 below). The remaining decisions (D1·D2·D3·D4·D5·D7) are inherited.
- **Precedent**: Same standing as making the TodoWrite tool·guidance built-in ([`docs/proposals/2026-06-01-builtin-todo-injection-design.md`](../../proposals/2026-06-01-builtin-todo-injection-design.md)) — the SDK assembles always-on capabilities at the engine boundary.

---

## 1. Goals / Non-Goals

### Goals
- **Personalization across session boundaries**: inject user/feedback/project/reference memories as an index at the start of a new session, for more personalized loop behavior.
- **always-on built-in**: the memory capability (tools + guidance + default store) is **always** on, with no caller wiring. Even if the caller does nothing, the agent can use memory.
- **Replaceable via a single interface**: a single `MemoryStore` owns both **the persistent backend + its tool surface**. When the caller injects its own `MemoryStore`, the default store and default tools are replaced **wholesale**.
- **Engine integration, loop unchanged**: only `FridayAgent` (the engine boundary) changes. `run_one_turn`·`assemble_system_prompt`·`LoopState`·`Tool` ABC·orchestrator have zero changes — the core loop stays unaware of memory and tool names.
- **Distributed integrity**: separate memory (Store) from conversation (LoopState). On distributed resume, the Store is re-injected per container (same as provider/tools) and `LoopState` serialization is unchanged.

### Non-Goals (YAGNI — inherited from proposal §7)
- **Sonnet prefetch ranking** (CC mechanism ②) excluded. index-only injection + on-demand read is sufficient. (Future extension point: `MemoryStore.search()`.)
- **Background fork extraction** (CC mechanism ③) excluded. friday has no resident process — replaced by inline self-directed saving.
- **Team memory·scope tags·secret scanning** excluded. Assumes a single store; multi-tenant namespacing is the responsibility of an external store override.
- **Per-turn index refresh** excluded. The index is a **session-start snapshot** (§5, D7).
- **`search` is not a default tool**. The default tools are the 3 of save/read/delete. A custom store can expose search by overriding `tools()`.
- **Session/checkpoint persistence** is separate from this design (handled by the existing `LoopState` serde).

---

## 2. Key Design Decisions

| # | Decision | Rationale |
|---|---|---|
| **D-INTEGRATION** | **always-on built-in + engine integration** (reverses proposal D6's "no engine changes") | User decision. Same standing as the TodoWrite built-in precedent — the capability is assembled at the engine boundary. However, the core *loop* still knows nothing about memory (injection happens only in `FridayAgent`). |
| **D-SEAM** | **A single `MemoryStore` class owns both persistence + `tools()`** | Requirement: "bundle store+tools into one interface-based class and inject it". Replace only the backend = implement only the persistence methods + inherit the default `tools()`; replace the tools too = override `tools()`. |
| **D-NAME** | Interface name = **`MemoryStore`** | "Provider" means the LLM completion backend in this codebase, which is confusing. "Store" clearly expresses persistence. Corrects the `MemoryProvider` mention at CLAUDE.md line 76 to this name·location. |
| **D-DEFAULT** | Default store = **`FileMemoryStore("FRIDAY_MEMORY.md")`** (lazy) | Locally/in demos you immediately see "memory actually being saved". Being lazy, construction alone creates no file. **Distributed persistence = inject a store backed by external Storage** (the default is for single-process/development use). |
| **D-TOOLS** | Default tools = **save / read / delete** | The index shows the full list, so a small set needs no search (on-demand read). delete removes wrong/stale memories. |
| D1 | Write = **agent inline self-directed saving** (dedicated tools) | Inherited from the proposal. |
| D2 | Read = **index injection + on-demand body read** | Inherited from the proposal. |
| D4 | Index = **auto-generated from store metadata** (`load_index()`) | Inherited from the proposal (single-step save). |
| D5 | Content model = **CC 4 types + Why/How + what-NOT-to-save + staleness** | Inherited from the proposal. |
| D7 | Index injection = **one-time snapshot at session start** (instance-lifetime cache) | Inherited from the proposal. `system_prompt` is session-static; fresh on every reconstruction during distributed resume. |

---

## 3. Module Structure — `friday_agent/memory/` (new)

```
friday_agent/memory/
  __init__.py  Re-exports public symbols (MemoryStore·FileMemoryStore·InMemoryStore·MemoryEntry·
               IndexEntry·MemoryType·MemorySave·MemoryRead·MemoryDelete·build_memory_section)
  store.py     MemoryType · MemoryEntry · IndexEntry · MemoryStore(ABC) ·
               FileMemoryStore · InMemoryStore
  tool.py      MemorySaveInput/ReadInput/DeleteInput · MemorySave · MemoryRead · MemoryDelete
  prompt.py    MEMORY_INSTRUCTIONS · render_index(entries) · build_memory_section(store)
```

| Part | Responsibility | Depends on |
|------|------|------|
| `MemoryStore` (ABC) | save/read/delete/load_index contract + default `tools()` implementation | `Tool` (lazy import) |
| `FileMemoryStore` | Parses·writes `FRIDAY_MEMORY.md` (default backend, lazy) | `MemoryStore` |
| `InMemoryStore` | dict-based (test double / low-cost single-process) | `MemoryStore` |
| `MemorySave/Read/Delete` | Agent surface — thin tools wrapping the store | `MemoryStore`, `Tool` |
| `build_memory_section` | Static instructions + auto index → injected text (async) | `MemoryStore` |

---

## 4. Data Model — `memory/store.py`

```python
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from friday_agent.tools.base import Tool


class MemoryType(str, Enum):            # CC 4 types
    user = "user"
    feedback = "feedback"
    project = "project"
    reference = "reference"


@dataclass
class MemoryEntry:                      # includes body (handled by read/save)
    name: str                           # stable identifier (kebab-case), upsert key
    description: str                    # one line — shown in the index, for judging relevance
    type: MemoryType
    body: str
    updated_at: float | None = None     # for freshness ("N days ago"), optional (epoch sec)


@dataclass
class IndexEntry:                       # metadata only (for injection — body excluded)
    name: str
    description: str
    type: MemoryType
    updated_at: float | None = None
```

---

## 5. `MemoryStore` ABC — Single Injection Seam

```python
class MemoryStore(ABC):
    """Single injection interface that owns both the persistent backend + its tool surface.

    Replace only the backend: implement only save/read/delete/load_index (inherit the default tools()).
    Replace the tools too: override tools().
    """

    # --- persistence (abstract, must implement) ---
    @abstractmethod
    async def save(self, entry: MemoryEntry) -> None: ...        # upsert by name (duplicate = update)
    @abstractmethod
    async def read(self, name: str) -> MemoryEntry | None: ...   # body on-demand
    @abstractmethod
    async def delete(self, name: str) -> None: ...
    @abstractmethod
    async def load_index(self) -> list[IndexEntry]: ...          # metadata-derived TOC (body not loaded)

    # --- tool surface (default implementation; override to replace the tool set) ---
    def tools(self) -> list["Tool"]:
        """Tools that expose this store to the agent. Default = save/read/delete wrapping self."""
        from friday_agent.memory.tool import MemorySave, MemoryRead, MemoryDelete
        return [MemorySave(self), MemoryRead(self), MemoryDelete(self)]
```

- **async**: same async contract as `Tool.call` · `LLMProvider.complete`. External Storage (network) fits in naturally.
- **upsert by name**: enforces "don't write duplicate memories — update the existing one" at the store level.
- **load_index ≠ read**: the index is metadata only (bounding injection cost). Kept separate so external stores can build the index cheaply server-side.
- **`tools()` lazy import**: avoids a circular import between `store.py`↔`tool.py`.

### 5.1 Default Implementation `FileMemoryStore(path="FRIDAY_MEMORY.md")`

A single-section file. A developer sees all memories by opening just one file.

```markdown
# FRIDAY_MEMORY.md
<!-- Friday agent memory. Auto-managed; safe to read and edit by hand -->

## [user] user-role
description: distributed backend engineer, 10 years of Go
updated: 2026-06-01T10:00:00
---
10 years of Go, new to this repo's frontend. Explain frontend via backend analogies.

## [feedback] testing-policy
description: integration tests use a real DB, no mocks
---
Rule: do not mock the DB in integration tests.
**Why:** last quarter, a prod migration failed after passing with mocks.
**How to apply:** when writing tests in this area.
```

- **Parsing rules**: `## [type] name` starts an entry → optional `description:` line → optional `updated: <iso>` line → `---` → body until the next `## ` or EOF.
- **lazy**: if the file is absent, `load_index()`/`read()` return an empty result/`None` (no file created). The file is **created on the first `save()`**. → Constructing `FridayAgent` alone does zero disk IO.
- **`save()`**: sets `updated_at` to `time.time()` (if unset), replaces the section with the same `name` (upsert), or appends it if absent. Rewrites the whole file.
- **`updated` (iso) ↔ `updated_at` (epoch)**: human-readable ISO in the file, epoch float in `MemoryEntry`. Converted on serialization/deserialization. If there is no `updated` line, `updated_at=None` (freshness caveat omitted).
- **Backend-agnostic behavior**: even though the file holds all the bodies, the store API keeps the **inject index only + body on-demand** contract (same behavior when overridden with external Storage).

### 5.2 `InMemoryStore`

Based on `dict[str, MemoryEntry]`. A test double + low-cost single-process option. Same `MemoryStore` interface (inherits the default `tools()`). Non-persistent.

---

## 6. Engine Integration — `friday_agent/core/engine.py` (Change Surface)

> Only `engine.py` changes. `core/loop.py`·`tools/orchestrator.py`·`api/prompts.py`·`core/state.py`·`tools/base.py` are **byte-unchanged**.

### 6.1 `__init__` — Default Store + Tool Registration + Collision Check

```python
# new argument (after max_concurrency)
memory: MemoryStore | None = None,
...
self._memory = memory if memory is not None else FileMemoryStore()
caller_tools = tools if tools is not None else []
builtins = builtin_tools()                  # TodoWrite
memory_tools = self._memory.tools()         # save/read/delete (or custom)
assembled = [*caller_tools, *builtins, *memory_tools]

# Verify that names are unique across the full assembled tool list (duplicate = ValueError).
# The LLM API rejects duplicate tool names, so preserve integrity with explicit rejection instead of a silent dedupe.
seen, dups = set(), []
for t in assembled:
    if t.name in seen:
        dups.append(t.name)
    seen.add(t.name)
if dups:
    raise ValueError(
        f"FridayAgent: duplicate tool names {sorted(set(dups))}. "
        f"TodoWrite and the active MemoryStore's tools are SDK-managed and always "
        f"registered; remove the colliding tool(s) or override the store's tools()."
    )

self._provider = provider
self._tools = assembled
self._system_prompt = system_prompt
self._config = config
self._max_concurrency = max_concurrency
self._memory_section: str | None = None      # session-start snapshot cache (lazy; async, so it cannot be built in __init__)
```

- The existing TodoWrite-only collision check (`clash`) is **generalized into a uniqueness check across all tools** (covers caller↔builtin, caller↔memory, and memory↔builtin).

### 6.2 `step()` — Async Memory Section Assembly + Prompt Concatenation

```python
async def step(self, state: LoopState) -> AsyncGenerator[...]:
    if self._memory_section is None:                                  # once (instance lifetime)
        self._memory_section = await build_memory_section(self._memory)
    effective_prompt = (
        f"{self._system_prompt}\n\n{self._memory_section}"
        if self._system_prompt else self._memory_section
    )
    tool_schemas = [tool.get_tool_schema() for tool in self._tools]
    async for item in run_one_turn(
        provider=self._provider,
        tools=self._tools,
        tool_schemas=tool_schemas,
        state=state,
        system_prompt=effective_prompt,        # base + memory section
        config=self._config,
        max_concurrency=self._max_concurrency,
    ):
        yield item
```

- Final system prompt order: **base → memory section → GENERAL_AGENT_GUIDANCE → TODO_GUIDANCE** (`assemble_system_prompt` inside `run_one_turn` appends the last two).
- `run_one_turn` signature/body unchanged — it merely passes the concatenated string to the existing `system_prompt: str` argument.

### 6.3 `compact()` — No Memory Section Injection

`compact()` calls `compact_conversation()` directly and does not go through `step()`'s prompt path → **the memory section·index do not leak into the summary** (same philosophy as the GENERAL/TODO guidance not leaking into the summary). `compact()` is not changed (verification only).

---

## 7. Injected Text — `memory/prompt.py`

```python
MEMORY_INSTRUCTIONS: str = """# Memory
You have a persistent memory that survives across sessions, accumulated over time.
Use the memory tools to save and recall typed facts.

## Types of memory
 - user: who the user is (role, preferences, expertise).
 - feedback: how the user wants you to work (corrections, confirmed approaches). Include the why.
 - project: ongoing work/goals/constraints not derivable from code or git.
 - reference: pointers to external resources (URLs, dashboards, tickets).

## When to save
Save when you learn a durable fact in one of the four types. For feedback/project,
structure the body as the rule/fact, then **Why:** and **How to apply:** lines.
Reuse an existing `name` to UPDATE rather than duplicate.

## What NOT to save
Do not save what is derivable (code structure, architecture, git history), one-off
debugging fixes, or ephemeral conversation/run state.

## When to access
Read a memory when it is relevant or the user asks. If a memory names a file,
function, or flag, verify it still exists before relying on it — a memory saying X
does not guarantee X exists now. If the user says to ignore memory, act as if empty.

The memory index below is a session-start snapshot; memories you save this session
appear in your tool_result immediately but in the index only next session."""


def render_index(entries: list[IndexEntry]) -> str:
    if not entries:
        return ("## Current memory index\n"
                "Your memory is empty. Save memories as you learn about the user, "
                "their feedback, and the project.")
    lines = "\n".join(f"- [{e.type.value}] {e.name} — {e.description}" for e in entries)
    return f"## Current memory index\n{lines}"


async def build_memory_section(store: MemoryStore) -> str:
    index = await store.load_index()
    return f"{MEMORY_INSTRUCTIONS}\n\n{render_index(index)}"
```

- **Static instructions first, dynamic index last** — preserves the static prefix for caching.
- `build_memory_section` is always non-empty (even an empty store gets the "empty" notice) → always injected.

---

## 8. Tools — `memory/tool.py`

```python
class MemorySaveInput(BaseModel):
    name: str = Field(description="Stable kebab-case id; reuse the same name to UPDATE an existing memory")
    description: str = Field(description="One-line summary shown in the memory index; used to judge relevance later — be specific")
    type: MemoryType = Field(description="user | feedback | project | reference")
    body: str = Field(description="Memory content. For feedback/project, structure as the rule/fact, then **Why:** and **How to apply:** lines")

class MemoryReadInput(BaseModel):
    name: str = Field(description="The memory's stable name (as shown in the index)")

class MemoryDeleteInput(BaseModel):
    name: str = Field(description="The memory's stable name to delete")
```

| Tool | name | input | Behavior | concurrency_safe |
|------|------|-------|------|---|
| `MemorySave` | `memory_save` | name, description, type(enum), body | `store.save(MemoryEntry(...))` → `ToolResult(data="Saved <type> memory '<name>'.")` | `False` (mutating) |
| `MemoryRead` | `memory_read` | name | `store.read(name)` → body; attaches a staleness caveat if `updated_at` is older than 1 day (86400s). If missing, an `is_error` result | `True` (read-only) |
| `MemoryDelete` | `memory_delete` | name | `store.delete(name)` → `ToolResult(data="Deleted memory '<name>'.")` | `False` (mutating) |

- All three return only a plain `ToolResult(data=...)` — **no `state_effect`** (memory lives in the Store, not in LoopState).
- Tool description = `__class__.__doc__` (passed to the LLM) — write "what·when" thoroughly.
- **staleness caveat** (`MemoryRead`): if `updated_at` is older than 1 day, one line after the body — `(This memory was written N days ago; verify any named file/function/flag still exists before relying on it.)`. The current time is `time.time()` at call time.
- 3 separate tools (instead of a single `action` discriminator): rich per-action schemas are clearer to the LLM.

---

## 9. Data Flow

```
[Session start — FridayAgent(provider, ..., memory=None|store)]
  self._memory = store or FileMemoryStore()
  self._tools  = caller_tools + [TodoWrite()] + self._memory.tools()   # ValueError on collision
  self._memory_section = None                                          # lazy

[Turn loop — caller drives via step()]
  step() on first call: self._memory_section = await build_memory_section(self._memory)  # snapshot
  effective = base + memory section → run_one_turn(system_prompt=effective)
    └─ assemble_system_prompt: + GENERAL + TODO
    └─ the model sees the index
         ├─ memory_read(name) → store.read() → body (+staleness caveat)
         └─ memory_save({...}) → store.save() (upsert) → file/Store updated
                                 → tool_result (model is aware immediately)
  ContextOverflow → caller runs engine.compact() → conversation folds into 1 summary
                    (memory section not injected; Store preserved independently — unaffected)

[Next session / container B]
  conversation (LoopState) is serialized by the caller to external Storage (existing mechanism; memory bodies not included)
  Memory (Store) persisted in FRIDAY_MEMORY.md / external Storage
  → FridayAgent reconstructed → build_memory_section reassembled → accumulated memories injected fresh as the index
```

---

## 10. Invariant Preservation & Distributed Safety

- **Core loop unchanged**: `run_one_turn`/`orchestrator`/`assemble_system_prompt`/`LoopState`/`Tool` ABC unchanged. The loop knows nothing about memory or tool names (injection happens only in `FridayAgent`).
- **par-critical integrity**: memory tools go through the normal tool path (orchestrator), so zero new risk to `tool_use↔tool_result` integrity.
- **Distributed resume**: `MemoryStore` is not serialized into `LoopState` (serde unchanged; container-local re-injection, same as provider/tools). Container B reconstructs with the store + reassembles the section → index is fresh.
- **Compaction preservation**: even when `compact()` folds the conversation into a single summary, memory is preserved independently in the Store. Memory provides continuity across both ① in-session compaction and ② session boundaries.
- **Implication of the session-start snapshot**: being an instance-lifetime cache, memories saved across multiple steps in the same process do not appear in that instance's index (fresh on the next reconstruction). However, the agent becomes aware immediately via `tool_result`, so in-session consistency is preserved (proposal D7).
- **Limits of always-on harmlessness**: memory is now always on, so it is not "a no-op if unused". However, the default `FileMemoryStore` is lazy, so **zero disk IO before use**, and an empty store injects only the "empty" index.

---

## 11. Downstream Cleanup & Doc Sync

### 11.1 `scripts/run_agent.py` (Required)
- Reflect the memory built-in: no separate wiring needed. State "memory always-on (default `FileMemoryStore`→`FRIDAY_MEMORY.md`, persists across session boundaries)" in the banner/docstring.
- Optional: concisely display `memory_save`/`memory_read`/`memory_delete` tool_use in `_print_new_messages` (same style as the current TodoWrite special handling).

### 11.2 Docs (SSOT Sync)
- **New** `docs/architecture/08-memory.md` — memory subsystem chapter (modules·interface·injection·distribution).
- `docs/architecture/00-overview.md` — add a 1-line `memory/` entry to the module map; reflect it in the reading order/scope.
- `docs/architecture/02-tool-orchestration.md` §⑥ built-in note — add, besides TodoWrite, "the active `MemoryStore`'s `tools()` are also always registered (`ValueError` on name collision)".
- **CLAUDE.md**:
  - Correct line 76 `MemoryProvider` (optional·NoOp) → `MemoryStore` (always-on·replaceable).
  - Separate memory from the "LLM abstraction boundary = 3 interfaces" list and describe it as an **independent memory subsystem**.
  - Include "memory (persistence·index injection·self-directed saving)" in the implementation scope charter; reflect the non-goals (prefetch ranking·background extraction·team memory).
  - Add `memory/` (store·tool·prompt) to the package structure line.

---

## 12. Test Strategy (No API Key Required · fake provider)

### Unit
- **`FileMemoryStore`**: save→read round-trip; `load_index()` returns only metadata without bodies; upsert (re-saving the same name = update); delete; `updated_at` depending on presence of `updated`; **if the file is absent, load_index/read = empty result (no file created)**; file created on the first save; `updated` (iso)↔`updated_at` (epoch) conversion.
- **`InMemoryStore`**: same contract round-trip; non-persistent.
- **`MemoryStore.tools()`**: default = `memory_save/read/delete` instances; if a subclass overrides it, returns that set.
- **`MemorySave/Read/Delete.call`**: schema validation; delegation to the store; `MemoryRead` staleness caveat attached (>1 day)·not attached (≤1 day/None); reading a missing name = `is_error` result.
- **`build_memory_section`/`render_index`**: empty store → "empty" wording; with entries, `[type] name — description` lines; static instructions precede the dynamic index.

### Integration (fake provider)
- Default (no injection): `step()`'s `tool_schemas` include `memory_save/read/delete`; `received_system_prompts` include `MEMORY_INSTRUCTIONS`.
- Model emits a `memory_save` tool_use → call handled normally → reflected in the default `FileMemoryStore`/`InMemoryStore` (appears in the next `build_memory_section` index).
- Model calls `memory_read` → body returned; caveat on stale entries.
- **Replacement**: inject a custom `MemoryStore` (overridden `tools()` — e.g. including search) → its tools are registered instead of the default 3.
- **Collision**: inject a tool named `memory_save` (or `TodoWrite`) via caller `tools=` → `ValueError` in `__init__` (message includes the tool name).
- **compact cleanliness**: the prompt on the `compact()` path contains neither `MEMORY_INSTRUCTIONS` nor the index.

### Distributed
- Save in session A → persisted in the Store (file/memory) → in "container B", reconstruct `FridayAgent` + reassemble `build_memory_section` → index reflects it fresh. **Memory bodies do not leak into `LoopState.to_dict()`** (verify there is no memory key in serde).

### Regression
- Keep the entire existing test suite green (in particular, the generalized collision check must not break the TodoWrite case, and the compact cleanliness test is retained).
