# 08 — Memory (Persistent Memory Subsystem)

> **Related docs**: [00-overview](00-overview.md) · [01-core-loop](01-core-loop.md) · [02-tool-orchestration](02-tool-orchestration.md) · [03-llm-providers](03-llm-providers.md)

---

## ① Purpose

Keeps typed facts (user/feedback/project/reference) long-term across session boundaries. It is **opt-in** — with `FridayAgent(memory=None)` (default) the subsystem is not mounted (no tools, instructions, or index reminder), and when the caller injects a `MemoryStore`, that store and its `tools()` are mounted as a whole. (Design history: originally an always-on built-in, but switched to opt-in based on real-world measurement — zero tool calls across all sessions, constant token spend on every call.)

## ② Owned Files

| Path | Responsibility | Key symbols |
|---|---|---|
| `friday_agent/memory/store.py` | Persistence + tool-surface interface, default file backend, prompt fragment assembly (static instructions / dynamic index reminder) | `MemoryStore`, `FileMemoryStore`, `MemoryEntry`, `IndexEntry`, `MemoryType`, `MEMORY_INSTRUCTIONS`, `build_memory_reminder` |
| `friday_agent/memory/tool.py` | Agent surface (save/read/delete) | `MemorySave`, `MemoryRead`, `MemoryDelete` |

## ③ Single Injection Seam — `MemoryStore`

`MemoryStore` (ABC) owns both **persistence (save/read/delete/load_index) + `tools()`**. To swap only the backend, implement just the 4 persistence methods (inheriting the default `tools()`); to swap the tools too, override `tools()`. The default tools are `memory_save`/`memory_read`/`memory_delete` and return only plain `ToolResult`s (memory lives in the Store, not in LoopState).

## ④ Engine Integration (loop unchanged)

`FridayAgent.__init__` registers `store.tools()` alongside the built-in and caller tools only when a store is injected via the `memory=` argument (`ValueError` on name collision; with `memory=None` there are no memory tools or prompts). When a store is mounted, `step()` **injects the memory prompt split into static/dynamic parts**: the static instructions `MEMORY_INSTRUCTIONS` come before the base system prompt (general→specific — domain rules get the recency advantage; byte-stable within a session, so safe in the cached system prefix), while the live index is rendered async every turn by `build_memory_reminder()` and carried as a turn-local `<system-reminder>` only on `messages[-1]` (`run_one_turn`'s `turn_reminders` path, non-persistent in `LoopState`). With an empty store, no reminder block is created at all (the instructions handle the empty-state guidance). `assemble_system_prompt`, `LoopState`, and the orchestrator are unchanged. `compact()` never renders the live index, so it does not leak into the summary; by default it sends no memory prompt at all, and with `reuse_prefix=True` only the static `MEMORY_INSTRUCTIONS` rides along as part of the shared system prefix. From the prompt caching (always-on) perspective: even when `memory_save`/`delete` changes the index, the system prefix and conversation history caches survive intact, and only the reminder block outside the breakpoints changes — placing the index in system would let a single save invalidate the entire conversation cache (a cost measured under the old design).

## ⑤ Distributed Safety

`MemoryStore` is not serialized into `LoopState` (container-local re-injection, same as provider/tools). When container B reconstructs `FridayAgent` with the store, the reminder is re-rendered and the index reflects fresh state. The default `FileMemoryStore` is a single-process/local convenience — for true distributed persistence, inject a `MemoryStore` backed by external Storage.

## ⑥ Non-Goals

Sonnet prefetch ranking · background fork extraction · team memory/secret scanning/scope tags are excluded. (The index is re-rendered by `build_memory_reminder` on every `step()` — no caching.) `search` is not a default tool; a custom store can expose it via `tools()`.
