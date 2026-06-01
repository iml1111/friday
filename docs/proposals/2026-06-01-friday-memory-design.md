# Design Proposal: Integrating Persistent Memory

- **Date**: 2026-06-01
- **Target**: `friday_agent` (distributed agent loop SDK)
- **Status**: design approved · not implemented · verified against current code (`FridayAgent` constructor / `assemble_system_prompt` 1-arg / `Tool` ABC) (this document is a proposal and contains no implementation plan or code)
- **Source**: ports Claude Code's filesystem-based memory (`memdir` / `MEMORY.md` / 4-type taxonomy), adapted to friday's pure-tool · caller-driven · distributed structure

---

## 1. Background & Motivation

Claude Code's memory is a subsystem that **makes some context long-lived across session boundaries**. It consists of three mechanisms:

1. **Session-start index injection** — injects `MEMORY.md` (the memory index) and behavioral instructions into the system prompt.
2. **Mid-session prefetch of relevant memories** — a Sonnet side-query picks up to 5 memory files matching the user query and injects them as `<system-reminder>`.
3. **Background extraction at turn end** — a forked agent distills 4-type (user/feedback/project/reference) facts from the preceding conversation and saves them to files.

friday currently has **none** of this (verified: no memory abstraction · recall · persist anywhere in `friday_agent/`). The only persistence mechanism is compaction (lossy summarization). friday is also fundamentally different from Claude Code in structure:

- **caller-driven `step()`**: there is no resident background process → CC's "turn-end fork extraction" cannot be carried over as-is.
- **Session-static context**: `system_prompt`/`tools` are fixed as arguments to `FridayAgent.__init__` (`core/engine.py:45-67`). They are not a per-turn re-injection channel.
- **External Storage assumed**: all conversation sessions and memory live in external Storage. Storage communication is limited to **developer-provided Tools** (the library does not assume a filesystem).
- **Distributed serialization**: the conversation is transported between containers via `LoopState.to_dict/from_dict`, but provider/tools are re-injected container-locally.

So the goal is "to layer the **essence** of CC memory (long-lived typed facts + index injection + self-directed saving) on top of friday's constraints (pure tools · caller-driven · session-static · external Storage) **without engine changes**".

---

## 2. Goals / Non-Goals

### Goals
- **Personalization across session boundaries**: inject user/feedback/project/reference memories accumulated in past sessions as context at the start of a new session, for more personalized loop behavior.
- **External Storage abstraction**: abstract memory persistence behind a single interface (`MemoryStore`) that developers override with their own Storage. The default implementation is provided as a **human-openable file** (`FRIDAY_MEMORY.md`) so "how memory is actually stored" is immediately visible.
- **Self-directed saving**: the agent saves memory itself via dedicated tools during normal turns (no separate extraction process needed).
- **No engine changes**: without touching the `FridayAgent`/`run_one_turn`/`LoopState` core, implement memory purely as **composable library parts + caller wiring**.
- **Distributed integrity**: separate memory (Store) from conversation (LoopState) so that on distributed resume the Store is re-injected per container (treated the same as provider/tools) and LoopState serialization is unchanged.

### Non-Goals (YAGNI)
- **Sonnet prefetch ranking** (CC mechanism ②) is not included. index-only injection + agent on-demand read is sufficient. (Left as a future `MemoryStore.search()` extension point.)
- **Background fork extraction** (CC mechanism ③) is not included. friday has no resident process — replaced by inline self-directed saving. (Can be extended later with a caller-driven extraction pass.)
- **Team memory · scope tags · secret scanning** are out of scope. A single store is assumed; multi-tenant namespacing is left as the responsibility of the external store override.
- **Session/checkpoint persistence** is separate from this design. The existing `LoopState` serialization (caller-owned) handles it. This design covers only the memory store.
- **Per-turn index refresh** is not done. Since `system_prompt` is session-static, the index is a **session-start snapshot** (see §4.5 below).

---

## 3. Key Design Decisions (Summary)

| # | Decision | Rationale |
|---|---|---|
| D1 | Write = **agent inline self-directed saving** (dedicated tool call) | friday has no resident background; corresponds to CC's direct-write path. The trade-off (not saved if not called) is mitigated by strong `when_to_save` instructions |
| D2 | Read = **index injection + on-demand body read** | CC `MEMORY.md` model. Bounded token cost, no ranking side-query needed |
| D3 | Persistence = **`MemoryStore` ABC** (single override seam), default **`FileMemoryStore`→`FRIDAY_MEMORY.md`** | LLMProvider philosophy (working default + swap via injection); the file default is a reference for "making the storage format visible" |
| D4 | Index = **auto-generated from store metadata** (`load_index()`) | CC's manual 2-step save (body + index pointer) is **collapsed into 1 step**; eliminates index↔body drift |
| D5 | Content model = **port CC's 4 types + quality guardrails** | eval-validated taxonomy (user/feedback/project/reference) + Why/How body structure + what-NOT-to-save + staleness caveat |
| D6 | Integration = **caller-owned + pure injection** (no engine changes) | friday philosophy (just as the caller drives step/compact, the caller drives memory injection too); zero core impact when memory is not wired |
| D7 | Injection timing = **one-time snapshot at session start (construction)** | `system_prompt` is session-static as a `FridayAgent.__init__` argument; matches the "inject at loop start" requirement |

---

## 4. Design Details

### 4.1 Module Boundary — `friday_agent/memory/` (new)

A new module separate from the engine. If memory is not used, it is as if it did not exist (zero core impact).

```
friday_agent/memory/
  store.py     MemoryStore (ABC) · MemoryEntry · IndexEntry · MemoryType
               FileMemoryStore(default) · InMemoryStore(test double)
  tool.py      MemorySave · MemoryRead · MemoryDelete   (each implements Tool ABC)
  prompt.py    build_memory_section(store) · MEMORY_INSTRUCTIONS · render_index
```

| Part | Responsibility | Depends on |
|------|------|------|
| `MemoryStore` (ABC) | save/read/load_index/delete/(search) contract | None (pure interface) |
| `FileMemoryStore` | Parses · writes `FRIDAY_MEMORY.md` (default backend) | `MemoryStore` |
| `MemorySave/Read/Delete` | Agent surface — thin tools wrapping the store | `MemoryStore`, `Tool` |
| `build_memory_section` | Static instructions + auto index → injection text | `MemoryStore` |

### 4.2 Data Model — `memory/store.py`

```python
class MemoryType(str, Enum):           # CC 4 types
    user = "user"; feedback = "feedback"; project = "project"; reference = "reference"

@dataclass
class MemoryEntry:                      # includes body (handled by read/save)
    name: str                          # stable identifier (kebab-case), upsert key
    description: str                   # one line — shown in the index, for judging relevance later
    type: MemoryType
    body: str
    updated_at: float | None = None    # for freshness ("N days ago"), optional

@dataclass
class IndexEntry:                      # metadata only (for injection — body excluded)
    name: str
    description: str
    type: MemoryType
    updated_at: float | None = None
```

### 4.3 `MemoryStore` ABC — Single Override Seam (async)

```python
class MemoryStore(ABC):
    @abstractmethod
    async def save(self, entry: MemoryEntry) -> None: ...      # upsert by name (duplicate = update)
    @abstractmethod
    async def read(self, name: str) -> MemoryEntry | None: ... # body on-demand
    @abstractmethod
    async def delete(self, name: str) -> None: ...
    @abstractmethod
    async def load_index(self) -> list[IndexEntry]: ...        # metadata-derived TOC (body not loaded)
    async def search(self, query: str) -> list[IndexEntry]:    # optional — default = description/name substring
        results = await self.load_index()
        q = query.lower()
        return [e for e in results if q in e.name.lower() or q in (e.description or "").lower()]
```

- **async**: same async contract as `Tool.call` · `LLMProvider.complete`. External Storage (network) fits in naturally.
- **upsert by name**: enforces "do not write duplicate memories — update the existing one" (CC rule) at the store level.
- **load_index ≠ read**: the index is metadata only (bounding injection cost). Kept separate so external stores can build the index cheaply server-side.

### 4.4 Default Implementation `FileMemoryStore(path="FRIDAY_MEMORY.md")`

A single sectioned file. Developers see all memory by opening just one file. The store maps sections↔`MemoryEntry`, and `load_index()` collects only section headers + `description`.

```markdown
# FRIDAY_MEMORY.md
<!-- Friday agent memory. Auto-managed; safe to read and edit by hand -->

## [user] user_role
description: distributed backend engineer, 10 years of Go
---
10 years of Go, new to this repo's frontend. Explain frontend via backend analogies.

## [feedback] testing_policy
description: integration tests use a real DB, no mocks
---
Rule: do not mock the DB in integration tests.
**Why:** last quarter, a prod migration failed after passing with mocks.
**How to apply:** when writing tests in this area.
```

- **Parsing rules**: `## [type] name` starts an entry → `description:` line → `---` → body until the next `## ` or EOF. An optional `updated: <iso>` meta line provides freshness (if absent, the caveat is omitted — compatible with the format above).
- **Backend-agnostic behavior**: even though the file holds every body, the store API keeps the **inject index only + body on-demand** contract. That way agent behavior stays the same when it is swapped for an external Storage override.
- `InMemoryStore` remains only as a dict-based test double (single process, non-persistent).

### 4.5 Recall Injection — `build_memory_section` + `system_prompt` Splicing

`system_prompt` is **session-static** as a `FridayAgent.__init__` argument (`core/engine.py:49,65`). So the memory index is assembled **once at session start (agent construction)** and spliced into `system_prompt` (matches the "inject at loop start" requirement).

```python
async def build_memory_section(store: MemoryStore) -> str:
    index = await store.load_index()
    return f"{MEMORY_INSTRUCTIONS}\n\n{render_index(index)}"
```

Returned text structure — **static instructions first, dynamic index last** (preserves the static prefix for caching):

```
{MEMORY_INSTRUCTIONS}                  ← static, same every session

## Current memory index                ← dynamic, auto-generated from store.load_index()
- [user] user_role — distributed backend engineer, 10 years of Go
- [feedback] testing_policy — integration tests: real DB, no mocks
- [project] auth_rewrite — auth replacement is a compliance requirement
(if empty) "Your memory is empty. Save memories as you learn about the user, their feedback, and the project."
```

**Caller wiring** (no engine changes — constructor arguments only):

```python
store = FileMemoryStore("FRIDAY_MEMORY.md")
section = await build_memory_section(store)              # once at session start
agent = FridayAgent(
    provider=provider,
    tools=[MemorySave(store), MemoryRead(store), MemoryDelete(store), *other_tools],
    system_prompt=f"{base_prompt}\n\n{section}",          # session-static injection
    config=config,
)
# then the caller drives the turn loop with step(): async for item in agent.step(state): ...
```

The engine's `assemble_system_prompt(system_prompt)` appends `GENERAL_AGENT_GUIDANCE` after it, so the final order is `base → memory section → general guidance`.

> **Session-static implication**: even if the agent saves a new memory mid-session, the index reflects it only in the next session. However, the agent learns of what it just saved immediately via `tool_result`, so consistency within the current session is preserved. On distributed resume, container B reconstructs `FridayAgent` and assembles `build_memory_section` **again**, so the index freshly reflects the Store state at resume time (simpler than per-turn refresh, and fresh on every resume).

### 4.6 `MEMORY_INSTRUCTIONS` — Porting CC `memdir` Instructions (Collapsed to 1-Step Saving)

| Section | Content | friday changes |
|------|------|--------------|
| Intro | "persistent file memory, accumulated over time" | Same |
| **Types of memory** | 4 types + `when_to_save`/`how_to_use`/`body_structure` (Why·How) | `<scope>` tags removed (single store) |
| **What NOT to save** | No derivable facts (code · architecture · git) · debugging solutions · ephemeral state | Same |
| **How to save** | "call `memory_save` with name/description/type/body" | **CC 2-step → 1-step** (index is automatic; the agent does not touch the index) |
| **When to access** | When relevant · when explicitly asked · on an "ignore" directive (act as if empty) | "grep memory dir" → `memory_read`/`memory_search` |
| **Before recommending** | Verify memories that name a file/function/flag before recommending; "memory says X ≠ X exists now" | Same |

> CC's `plan/tasks comparison` section is **excluded** because friday's core has no plan/task concept. CC's "Searching past context" (transcript `.jsonl` grep) is **excluded** because sessions live in external Storage (caller-owned).

### 4.7 The 3 `MemoryTool`s — Agent Surface (`memory/tool.py`)

Thin tools wrapping the same store. Since the tool description = `__class__.__doc__` (sent to the LLM, `tools/base.py:60`), the docstrings faithfully spell out "what · when".

```python
class MemorySaveInput(BaseModel):
    name: str = Field(description="Stable kebab-case id; reuse the same name to UPDATE an existing memory")
    description: str = Field(description="One-line summary shown in the memory index; used to judge relevance later — be specific")
    type: MemoryType = Field(description="user | feedback | project | reference")
    body: str = Field(description="Memory content. For feedback/project, structure as the rule/fact, then **Why:** and **How to apply:** lines")

class MemorySave(Tool):
    """Save or update a long-term memory that persists across sessions. Use when you
    learn something about the user, their feedback on how to work, project context not
    derivable from code, or a pointer to an external system. Reuse an existing `name`
    to update rather than duplicate. Do NOT save code/architecture/git facts or
    ephemeral conversation state."""
    name = "memory_save"
    def __init__(self, store: MemoryStore) -> None: self._store = store
    def input_schema(self) -> type[BaseModel]: return MemorySaveInput
    async def call(self, args: dict) -> ToolResult:
        inp = MemorySaveInput(**args)
        await self._store.save(MemoryEntry(inp.name, inp.description, inp.type, inp.body))
        return ToolResult(data=f"Saved {inp.type.value} memory '{inp.name}'.")
```

| Tool | input | Behavior |
|------|-------|------|
| `memory_save` | name, description, type(enum), body | `store.save()` — the required type enum · description steer quality through the schema |
| `memory_read` | name | `store.read()` → body; attaches a staleness caveat if `updated_at` is over 1 day old (ported from CC `memoryFreshnessNote`) |
| `memory_delete` | name | `store.delete()` — supports "remove wrong/stale memories" |

- Returns only a plain `ToolResult(data=...)` — no `state_effect` needed (memory lives in the Store, not in LoopState).
- **3 separate tools** instead of a single tool with an `action` discriminator: rich per-action schemas are clearer to the LLM.
- `search` is an **optional** tool, exposed as `memory_search` only when the external store supports it.

---

## 5. Data Flow in One Session

```
[Session start]
  store = FileMemoryStore("FRIDAY_MEMORY.md")
  section = build_memory_section(store)            # instructions + auto index (snapshot)
  agent = FridayAgent(tools=[memory_*, ...], system_prompt = base + section)

[Turn loop — caller drives via step()]
  Model sees the index
    ├─ needs a relevant body → tool_use(memory_read, name) → store.read() → body (+freshness caveat)
    └─ save-worthy cue seen → tool_use(memory_save, {name,desc,type,body})
                              → store.save() (upsert) → FRIDAY_MEMORY.md updated
                              → tool_result (model aware immediately) → recorded in LoopState
  ContextOverflow → caller runs engine.compact() → conversation folds into 1 summary
                    (memory preserved independently in Store — unaffected)

[Session end / next session]
  Conversation (LoopState) serialized by caller to external Storage (existing mechanism)
  Memory (Store) persisted in FRIDAY_MEMORY.md / external Storage
  → section reassembled at next session start → accumulated memories injected as the index
```

Key point: **conversation (LoopState) and long-term memory (Store) are separate stores**. The memory tools' `tool_result`s remain in LoopState, but the actual memory bodies live in the Store.

---

## 6. Invariant Preservation & Distributed Safety

- **No engine changes**: none of `FridayAgent`/`run_one_turn`/`LoopState`/`Tool` ABC is modified. Memory is just the new `memory/` module + caller wiring.
- **tool_use ↔ tool_result integrity**: memory tools take the same path (`orchestrator`) as ordinary tools, so they add no new risk to the integrity invariant.
- **Distributed resume**: the Store is not serialized into `LoopState` (container-local re-injection, same as provider/tools). Container B reconstructs `FridayAgent` with a store pointing to the same external Storage and reassembles `build_memory_section` → the index freshly reflects the state at resume time. LoopState serialization is unchanged.
- **Preserved across compaction**: even when `engine.compact()` folds the conversation into a single summary, memory is preserved independently in the Store. Memory provides **continuity across both** ① in-session compaction and ② session boundaries (the real payoff of personalization).
- **Incremental adoption · harmlessness**: if the memory tools are not registered in `tools` and `section` is not injected, friday behaves exactly as before (no-op). If memory is wired but `FRIDAY_MEMORY.md` does not exist yet, `FileMemoryStore` returns an empty index and creates the file on the first `memory_save`.
- **Resume characteristics per backend**: `InMemoryStore` = single process only, `FileMemoryStore` = survives if the file is on a shared volume, **external Storage override = the real distributed answer**.

---

## 7. Explicit Boundaries (Excluded from This Design)

| Excluded item | Reason | Future room |
|----------|------|----------|
| Sonnet prefetch relevant-memory ranking | Chose index-only + on-demand (D2) | `MemoryStore.search()` extension point |
| Background fork extraction | friday has no resident process (D1 inline write) | Can add a caller-driven extraction pass |
| Team memory · secret scanning · scope tags | Single store; namespacing is the store's responsibility | Handled by the external store |
| Session/checkpoint persistence | Existing `LoopState.to_dict` mechanism (caller-owned) | Separate from the memory design |
| Per-turn index refresh | `system_prompt` is session-static (D7) | Reassembly on each resume is sufficient |

---

## 8. CC → friday Mapping (Traceability)

| Claude Code | friday | Nature |
|---|---|---|
| `~/.claude/projects/<slug>/memory/` filesystem | `MemoryStore` ABC (default `FileMemoryStore`→`FRIDAY_MEMORY.md`) | Abstraction |
| `MEMORY.md` manual index (2-step save) | Auto index from store metadata (1 step) | **Improvement** |
| 4 types + Why/How + what-NOT-to-save | Ported into `MEMORY_INSTRUCTIONS` | Same (scope tags removed) |
| System prompt injection by the harness | `build_memory_section` (caller splices it into `system_prompt`) | harness → caller |
| Sonnet prefetch ranking | index-only + on-demand read | **Excluded** |
| Turn-end fork extraction | Inline `memory_save` | **Excluded/replaced** |
| Memory access via generic tools (Read/Edit/Write/Grep) | Dedicated `memory_save`/`read`/`delete` tools | FS-independent |
| `memoryAge`/`memoryFreshnessNote` freshness | `updated_at` + read-time caveat | Ported |
| Team memory/secret scanning | (excluded) | Single store |

---

## 9. Change Surface (touch map)

| File | Change | Nature |
|---|---|---|
| `friday_agent/memory/store.py` | **New** — `MemoryStore`/`MemoryEntry`/`IndexEntry`/`MemoryType`/`FileMemoryStore`/`InMemoryStore` | Abstraction + default backend |
| `friday_agent/memory/tool.py` | **New** — `MemorySave`/`MemoryRead`/`MemoryDelete` (+optional `MemorySearch`) | Agent surface |
| `friday_agent/memory/prompt.py` | **New** — `MEMORY_INSTRUCTIONS`·`build_memory_section`·`render_index` | Injection text |
| `friday_agent/core/*` | **No changes** | No engine changes (the core of the design) |
| `scripts/` (example) | Caller wiring example (memory tools + section injection in the constructor) | Integration demo |
| `tests/` | Unit + integration (distributed resume) | Verification |
| `docs/architecture/` | New chapter describing the memory module (or 1 line in the 00-overview module map) | Docs sync |

> Contrast: the todo-tracking proposal **changed the loop · engine** via `state_effect` · reminder injection. Memory uses only the `system_prompt`/`tools` constructor arguments and the tool path, so there are **zero core changes** — this is the direct benefit of choosing D6 (caller-owned).

---

## 10. Test Strategy

- **Unit**
  - `FileMemoryStore`: section parsing round-trip (save→read), `load_index()` returns metadata only without bodies, upsert (re-saving the same name updates it), freshness depending on whether `updated:` is present.
  - `MemorySave/Read/Delete.call`: schema validation, delegation to the store, read attaches the staleness caveat (>1 day).
  - `build_memory_section`: empty store → "empty" message, per-type index lines when entries exist; static instructions come before the dynamic index.
  - `search` (default): description/name substring matching.
- **Integration (fake provider)**
  - Model calls `memory_save` → section appears in `FRIDAY_MEMORY.md` → index reflected in the next session's `build_memory_section`.
  - Model calls `memory_read` → body returned, with a caveat on stale entries.
- **Distributed**
  - Save in session A → persisted in the Store (file/external) → reconstruct `FridayAgent` + reassemble `build_memory_section` in "container B" → index freshly reflected. Memory bodies do not leak into LoopState serialization (separation verified).
- **Harmlessness**
  - Memory parts not wired → friday behaves the same as before (no-op).
