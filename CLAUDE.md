# CLAUDE.md

## Project Purpose

A **domain-agnostic + LLM-agnostic** SDK for **running agent loops on the cloud/server side**, built by analyzing how several agent loops (agentic loops) work. The goal is to serve as a foundation for building AI agents for a wide range of purposes beyond programming.

## Current Status

**Implementation complete** — Phase 1~3 (minimal loop · tool orchestration · context management) are all implemented in the `friday_agent/` package. Includes real backend adapters (Anthropic · OpenAI) + model-prefix routing, and stateless distributed resume (LoopState). The architecture docs (`docs/architecture/`) are authoritative.

- Install: `pip install -e ".[dev]"`  ·  Tests (no API key required, fake provider): `python -m pytest`
- Real API verification (incurs token cost): `LLM_MODEL=<model-id> python scripts/verify/verify_p2.py` (P2~P4)
- For code entry points · module map · reading order, see `docs/architecture/00-overview.md` (especially `#module-map`).

## In-Progress Work — `refactor/loop-contracts` (remove when done)

Branch `refactor/loop-contracts` adds seven loop-contract changes: a summary-tag fix, `Terminal.state`, an OpenAI tool-message ordering fix, `turn_sections`, image tool results, deferred tools (`Suspended`/`resume`), and `compact(reuse_prefix=)`.

- **Spec** (approved): `docs/superpowers/specs/2026-10-03-loop-contracts-design.md` — the decisions and their reasons (D1–D15).
- **Plan** (written, awaiting the user's review): `docs/superpowers/plans/2026-10-03-loop-contracts.md` — 12 tasks with code, tests, doc edits, and commit commands. Its Global Constraints apply to every task.
- **Status**: spec and plan committed; Task 1 not started. The user has not chosen an execution method yet — ask first (recommended: native, i.e. `superpowers:executing-plans` in order, then one fresh whole-branch review).
- **Resume**: on `refactor/loop-contracts`, confirm `python -m pytest -q` → 253 passed, then start at Task 1.
- Task 11 (real-API checks) needs the user's go-ahead (token cost). Task 12 deletes the spec, the plan, and this section.

## Architecture Docs = Single Source of Truth

`docs/architecture/` is the authoritative documentation describing the current implementation (`friday_agent/`) subsystem by subsystem. Implementation and maintenance are based on these docs.

**Reading order for understanding** (all below are under `docs/architecture/`):
1. `00-overview.md` — purpose·big picture·module map·scope
2. `01-core-loop.md` — single-turn loop·termination/recovery·resume
3. `02-tool-orchestration.md` — partitioning·parallel execution
4. `03-llm-providers.md` — LLM boundary·adapters
5. `04-context-compaction.md` — caller-driven compact
6. `05-messages.md` — messages/conversion
7. `06-invariants.md` — par-critical invariants
8. `07-data-models.md` — catalog of all data models
9. `08-memory.md` — persistent memory subsystem (opt-in, `MemoryStore` injection)

## Architecture Big Picture

The core is a turn loop **driven by the caller** on top of `run_one_turn()` (single turn) (see `docs/architecture/01-core-loop.md`, `02-tool-orchestration.md`):

```
Caller (distributed orchestrator / server side)
        │  LoopState(messages=[...])  ← caller builds the first turn
        ▼
engine.step(state)       ┌─ run_one_turn once (async generator) ──────────┐
        │                │ 1. state.messages → LLM API call               │
        │                │ 2. receive response → branch on stop_reason    │
        │                │      end_turn  → Terminal (loop ends)          │
        │                │      tool_use  → run tools → tool_result       │
        │                │ → stream-yield Message…s in order              │
        │                │ → lastly yield one LoopState | Terminal        │
        │                │   sentinel                                     │
        │                └────────────────────────────────────────────────┘
        │   on ContextOverflowError → caller runs engine.compact(state), then retries
        ▼
Caller consumes via `async for`: if the last sentinel is a LoopState, call step() again with it as-is; if Terminal, stop.
```

> Implementation note: the library exposes only single-turn execution, `FridayAgent.step()` — the batch driver (`query()`)·run-to-completion entry point (`submit_message`)·`max_turns` were removed (the while-true driver of agent loop spec `02` is externalized to the caller). It emits a serializable `LoopState` as-is at the turn boundary to support **stateless distributed resume** (`FridayAgent.step()` + the types' `to_dict()`/`from_dict()`).

Core data structures (`docs/architecture/01-core-loop.md`·`05-messages.md`):
- **`Message` + `ContentBlock`** — there is no separate *Message class union. Just a single `Message` dataclass (`type` tag: `user`/`assistant`/`system`) with flag fields (`is_compact_summary`/`is_meta`/`is_api_error_message`). Blocks are likewise represented by a single flat `ContentBlock` dataclass (`type`: `text`/`tool_use`/`tool_result`/`thinking`).
- **Terminal** — the loop termination reasons **that `run_one_turn` actually emits** (`completed`, `model_error`). Context overflow propagates to the caller as `ContextOverflowError`, so there is no `prompt_too_long` termination reason. "Continue" at a turn boundary is expressed by emitting the `LoopState` as-is (in contrast to `Terminal`).
- **LoopState** (implementation) — the serializable loop state (messages·turn_count) and also the turn-boundary "continue" resume sentinel. It is the transport unit for distributed resume and owns its own JSON serde (`to_dict()`/`from_dict()`) (`core/state.py`·`messages/types.py`). The transport unit is `json.dumps(loopstate.to_dict())`.

## Implementation Scope Charter (must follow)

The architecture docs intentionally describe only **"the essence of the agent loop algorithm"** — a higher level of abstraction than the actual agent loop implementations analyzed. The excluded items below exist in those implementations but are out of scope for this SDK. **Do not re-add these on your own** (details: `docs/architecture/00-overview.md`):

| Included (implemented at par level) | Excluded (intentional) |
|---|---|
| while-true loop + stop_reason branching, all termination/recovery paths | Subagent delegation |
| Tool partitioning + concurrency (parallel/sequential batches) | Streaming / incremental display UX |
| External compact + overflow propagation (caller-driven compact) | Context optimizations such as Snip·Micro·Collapse |
| System prompt assembly machinery | Model fallback · Beta headers |
| LLM-agnostic provider boundary | Vendor build modes (ant/REPL/SIMPLE) |
| Prompt caching (system+tools+conversation history, always-on; Anthropic explicit breakpoints / OpenAI automatic) | mega-turn (>20 blocks) intermediate breakpoints · TTL settings · OpenAI `prompt_cache_key` |

**Par-critical integrity**: if a `tool_use`↔`tool_result` pair is broken, the LLM API rejects the request. This integrity must be preserved on every path, whether recovery or parallel execution (details: `docs/architecture/06-invariants.md`).

## LLM Abstraction Boundary

Swapping the LLM backend is confined to **3 interfaces** (`docs/architecture/03-llm-providers.md`):
- **`LLMProvider`** — the only *required* swap point. Performs the completion call + normalizes the response into `AssistantResponse`.
- **`ToolExecutor`** / **`ContextManager`** — mostly generic (swap only the LLM call site).
- **Memory is a separate subsystem** (not the LLM boundary) — `MemoryStore` in `friday_agent/memory/` (owns persistence + `tools()`). opt-in (not mounted by default); mounted when a store is injected. Details: `docs/architecture/08-memory.md`.

For the vendor boundary and per-adapter differences, see the adapter differences table in `docs/architecture/03-llm-providers.md`.

## Implementation Roadmap

The code was implemented in Phase order (each Phase verified independently):
- **Phase 1 — Minimal loop**: single tool call → result → response. `messages/types.py`, `core/state.py`, `tools/base.py`, `tools/builtin/`, `api/provider.py`+adapters, `core/loop.py`.
- **Phase 2 — Tool orchestration**: partitioning + parallel execution (`asyncio.gather` + `Semaphore`) + block order preservation. `tools/orchestrator.py`.
- **Phase 3 — Context management**: external compact + overflow propagation (caller-driven). When `step()` raises `ContextOverflowError`, the caller shrinks the context with `engine.compact(state)` and retries. `context/compact.py` (summarization only), `messages/normalize.py`. For compaction behavior details, see `docs/architecture/04-context-compaction.md`.

Actual package structure (`friday_agent/`): `core/`(loop·engine·state) · `tools/`(base·orchestrator·builtin) · `context/`(compact — summarization only, no recovery.py) · `api/`(provider·configs·anthropic_provider·openai_provider·prompts) · `messages/`(types·normalize) · `memory/`(store·tool). For per-file responsibilities, see `docs/architecture/00-overview.md#module-map`.

## Target Stack & Verification

- **Python 3.11+**, `anthropic` + `openai` SDK + `pydantic` + `anyio`.
- API keys: **external injection only** (required). The library does not read environment variables — pass the key directly to the adapter (`AnthropicProvider(api_key=...)`/`OpenAIProvider(api_key=...)`) when creating the provider. Key resolution (env→argument) and model prefix (claude-/gpt-)→adapter routing are both handled by the boundary layer `scripts/_env.py` (`resolve_api_key(model)`·`create_provider(model, api_key=...)`·`create_config(model, ...)`) — the library provides no routing factory. **`FridayAgent` takes the provider directly (required)** — there are no model/api_key arguments. Public API surface: `engine.step(state)` (single-turn async generator — yields Messages in order, then finally yields one `LoopState | Terminal` sentinel) + `engine.compact(state)` (caller-driven compact). The call config is passed directly as the vendor config (e.g. `AnthropicConfig(max_tokens=...)` or `provider.config_type(max_tokens=...)`; defaults to `provider.config_type()` if unspecified). `context_window`·`max_output_tokens` go to the adapter constructor, `max_concurrency` to `FridayAgent(...)`. Keep the context injection surface simple — static content is a single `system_prompt` string (composing sections is the caller's job), and there are no engine-level dynamic injection hooks. The compaction prompt is a separate surface that `system_prompt` does not reach, so it takes a single `compact_instructions` string separately (default `""` = prompt unchanged).
- **Built-in todo always-injected**: the `TodoWrite` tool and todo guidance (`TODO_GUIDANCE`) are always auto-registered·injected as built-ins (no caller injection needed, no opt-out). If the caller passes a tool with the same name, `FridayAgent.__init__` rejects it with `ValueError`.
- **memory is opt-in**: with `FridayAgent(memory=None)` (default), the memory subsystem is not mounted — none of the 3 tools (`memory_save`/`memory_read`/`memory_delete`), instructions, or index reminder. A store must be explicitly injected, e.g. `FridayAgent(..., memory=FileMemoryStore())`, to mount it (the store itself is the tool surface). When mounted, injection is split static/dynamic — `MEMORY_INSTRUCTIONS` (static instructions) goes into the system prompt in `step()`, and the live index is carried by `build_memory_reminder` as a turn-local `<system-reminder>` (`messages[-1]` only, non-persistent) (preserves the cache prefix). The core loop·`LoopState` serde are unchanged (the store is re-injected container-locally).
- Phase verification: single tool (P1) → parallel tool batch (P2) → `ContextOverflowError` → caller recovery via `engine.compact(state)` (P3).

## Key Implementation Pitfalls

- `step()` sends `state.messages` to the API as-is. Context window management is the caller's responsibility — when `step()` raises `ContextOverflowError`, shrink with `engine.compact(state)` and retry.
- The `role` of a `tool_result` message must be `"user"`, and the first message must also be user (role alternation rule).
- `tool_use.input` arrives already parsed as a dict (per the Anthropic SDK) — do not JSON-parse it yourself. (However, the OpenAI adapter parses the JSON-string arguments and normalizes them to a dict — a vendor-specific difference.)
- Do not send `temperature` when thinking is enabled. For an empty `tools=[]`, omit the field entirely.
- Prompt caching is **always-on** (no opt-out·config knob): the Anthropic adapter's `_apply_cache_control` places `cache_control:{ephemeral}` on every call's last system block (=tools+system) + the last **persistent** block of the last/second-to-last message (system+tools prefix + conversation history cache). Trailing `<system-reminder>` reminders are skipped — breakpoints on non-persistent blocks are not reusable. `messages[-2]` is the stable anchor — because per-turn reminders (todo·memory index) are attached only to `messages[-1]`. OpenAI caches automatically, so its adapter is unchanged.
- Even with parallel execution, results are yielded **in tool_use block order** (`asyncio.gather` preserves argument order).
