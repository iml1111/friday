# 08 — Memory (Persistent Memory Subsystem)

> **Related docs**: [00-overview](00-overview.md) · [01-core-loop](01-core-loop.md) · [02-tool-orchestration](02-tool-orchestration.md) · [03-llm-providers](03-llm-providers.md)

---

## ① Purpose

Keeps typed facts (user/feedback/project/reference) long-term across session boundaries. It is an **always-on built-in**, and when the caller injects its own `MemoryStore`, the default store and tools are replaced as a whole.

## ② Owned Files

| Path | Responsibility | Key Symbols |
|---|---|---|
| `friday_agent/memory/store.py` | Persistence + tool-surface interface, default/in-memory backends | `MemoryStore`, `FileMemoryStore`, `InMemoryStore`, `MemoryEntry`, `IndexEntry`, `MemoryType` |
| `friday_agent/memory/tool.py` | Agent surface (save/read/delete) | `MemorySave`, `MemoryRead`, `MemoryDelete` |
| `friday_agent/memory/prompt.py` | Instructions + auto-index injection text | `MEMORY_INSTRUCTIONS`, `build_memory_section`, `render_index` |

## ③ Single Injection Seam — `MemoryStore`

`MemoryStore` (ABC) owns both **persistence (save/read/delete/load_index) + `tools()`**. To swap only the backend, implement just the 4 persistence methods (inheriting the default `tools()`); to swap the tools too, override `tools()`. The default tools are `memory_save`/`memory_read`/`memory_delete` and return only plain `ToolResult`s (memory lives in the Store, not in LoopState).

## ④ Engine Integration (loop unchanged)

`FridayAgent.__init__` sets up a default `FileMemoryStore` and registers `store.tools()` alongside the built-in and caller tools (`ValueError` on name collision). Every turn, `step()` assembles `build_memory_section()` async (rebuilt per turn, no caching) and appends it after the base system prompt. `run_one_turn`, `assemble_system_prompt`, `LoopState`, and the orchestrator are unchanged. `compact()` does not inject the memory section, so the index does not leak into the summary.

## ⑤ Distributed Safety

`MemoryStore` is not serialized into `LoopState` (container-local re-injection, same as provider/tools). When container B reconstructs `FridayAgent` with the store, the section is reassembled and the index reflects fresh state. The default `FileMemoryStore` is a single-process/local convenience — for true distributed persistence, inject a `MemoryStore` backed by external Storage.

## ⑥ Non-Goals

Sonnet prefetch ranking · background fork extraction · team memory/secret scanning/scope tags are excluded. (The index is reassembled by `build_memory_section` on every `step()` — no caching.) `search` is not a default tool; a custom store can expose it via `tools()`.
