# 00. Overview

Entry-point document for grasping the whole architecture of the `friday_agent/` package at a glance.  
Detailed implementation is covered in the numbered sub-documents (01–08).

---

## Project Purpose

A **domain-agnostic + LLM-agnostic** SDK for **running agent loops in the cloud / on the server side**,  
built by analyzing the operating structure of several agentic loops.  
The goal is to serve as a foundation for building AI agents for a wide range of purposes beyond programming.

Implementation: `friday_agent/` package (Python 3.11+, `anthropic` + `openai` SDK + `pydantic` + `anyio`).

---

## Big Picture: Caller-Driven Turn Loop

The core design principle is that **the library exposes only single-turn execution (`FridayAgent.step()`) and externalizes the while-true driver to the caller**. This lets the same engine be reused across diverse server-side execution contexts such as distributed orchestrators and serverless (local example driver: `scripts/run_agent.py`).

```
Caller (distributed orchestrator / server side)
   │  LoopState(messages=[...])
   ▼
async for item in engine.step(state):   ← AsyncGenerator
   │   run_one_turn(state) drives it internally
   │     1. normalize → provider.complete()
   │     2. stop_reason branch
   │          end_turn → Terminal(completed)
   │          tool_use → run_tools → tool_result
   │
   ├─ yield: AssistantMessage          ← immediately on response arrival
   ├─ yield: tool_result Message…      ← each tool result
   └─ yield: LoopState | Terminal      ← exactly 1 final sentinel
        On ContextOverflowError → caller runs engine.compact(state), then retries

If item is LoopState, call step() again with it as-is; if Terminal, stop.
```

Emitting the serializable `LoopState` as-is at each turn boundary supports **stateless distributed resume**.

In addition, `FridayAgent` always registers and injects **TodoWrite (todo tracking)** and **memory (`MemoryStore`)** as built-ins with no caller wiring — see [02-tool-orchestration](02-tool-orchestration.md) · [08-memory](08-memory.md) for details.

---

## Module Map

| Subsystem | Files | Doc |
|---|---|---|
| `core/` | `loop.py`·`engine.py`·`state.py` | [01-core-loop](01-core-loop.md) |
| `tools/` | `base.py`·`orchestrator.py`·`builtin/example_tool.py`·`builtin/todo_write.py` | [02-tool-orchestration](02-tool-orchestration.md) |
| `api/` | `provider.py`·`configs.py`·`anthropic_provider.py`·`openai_provider.py`·`prompts.py` | [03-llm-providers](03-llm-providers.md) |
| `context/` | `compact.py` | [04-context-compaction](04-context-compaction.md) |
| `messages/` | `types.py`·`normalize.py` | [05-messages](05-messages.md) |
| (cross-cutting) | par-critical invariants | [06-invariants](06-invariants.md) |
| (cross-cutting) | catalog of all data models | [07-data-models](07-data-models.md) |
| `memory/` | `store.py`·`tool.py` — persistent memory subsystem. always-on built-in, replaced via `MemoryStore` injection | [08-memory](08-memory.md) |

---

## Reading Order

00 → 01 → 02 → 03 → 04 → 05 → 06 (→ 07 for reference) (→ 08 memory)

| Order | Doc | Key Content |
|---|---|---|
| 00 | This doc | Purpose · big picture · module map · scope |
| 01 | [01-core-loop](01-core-loop.md) | `run_one_turn()`, `FridayAgent.step()`, state flow |
| 02 | [02-tool-orchestration](02-tool-orchestration.md) | Tool partitioning · parallel/sequential execution · order preservation |
| 03 | [03-llm-providers](03-llm-providers.md) | `LLMProvider` abstraction · Anthropic · OpenAI adapters |
| 04 | [04-context-compaction](04-context-compaction.md) | `ContextOverflowError` · `engine.compact()` · summarization strategy |
| 05 | [05-messages](05-messages.md) | Message union type · `normalize_for_api()` |
| 06 | [06-invariants](06-invariants.md) | par-critical invariants · `tool_use`↔`tool_result` integrity |
| 07 | [07-data-models](07-data-models.md) | Catalog of all data model fields · serialization boundaries (reference) |
| 08 | [08-memory](08-memory.md) | Persistent memory subsystem · `MemoryStore` replacement seam · distributed safety |

---

## Implementation Scope Charter

The spec intentionally describes only **"the essence of the agent loop algorithm"**. The excluded items below exist in the real implementations analyzed but are outside this SDK's scope. **Do not re-add them arbitrarily**.

| Included (implemented at par level) | Excluded (intentional) |
|---|---|
| while-true loop + stop_reason branching, all termination/recovery paths | Subagent delegation |
| Tool partitioning + concurrency (parallel/sequential batches) | Streaming / incremental display UX |
| External compact + overflow propagation (caller-driven compact) | Context optimizations such as Snip·Micro·Collapse |
| System prompt assembly machinery | Model fallback · Beta headers |
| LLM-agnostic provider boundary | Vendor build modes (ant/REPL/SIMPLE) |
| Prompt caching (system+tools+conversation history, always-on; Anthropic explicit breakpoints / OpenAI automatic) | mega-turn (>20 blocks) intermediate breakpoints · TTL settings · OpenAI `prompt_cache_key` |

**Par-critical integrity**: if a `tool_use`↔`tool_result` pair is broken, the LLM API rejects the request. This integrity must be preserved on every path, including recovery and parallel execution. See [06-invariants](06-invariants.md) for details.

---

## Verification · Test Entry Points

```bash
# No API key required — full test suite with fake provider
python -m pytest

# Real API verification (calls real backends — incurs token cost)
LLM_MODEL=<model-id> python scripts/verify/verify_p2.py   # tool orchestration
LLM_MODEL=<model-id> python scripts/verify/verify_p3.py   # context overflow · compact recovery
LLM_MODEL=<model-id> python scripts/verify/verify_p4.py   # real backend end-to-end · adapter swap demonstration

# Run the local example driver
python scripts/run_agent.py
```

---

## Design Rationale (Why) Summary

The library exposes only the single-turn `FridayAgent.step()` and externalizes the while-true driver to the caller. This decision has two key benefits.

1. **Stateless distributed resume** — since the serializable `LoopState` is emitted as-is at every turn boundary, state can be restored even across process restarts or in distributed-queue environments. JSON serde is handled by the types' (`LoopState`/`Message`) `to_dict()`/`from_dict()` methods.
2. **Separation of context-management responsibility** — propagating `ContextOverflowError` to the caller keeps the library internals simple and lets the caller directly control the compact strategy (timing · summarization method) (`engine.compact(state)`).
