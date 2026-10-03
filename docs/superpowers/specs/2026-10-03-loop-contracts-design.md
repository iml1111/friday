# Design Spec: Loop Contract Extensions

- **Date**: 2026-10-03
- **Branch**: `refactor/loop-contracts` (from `main` @ `c200c22`, v0.5.0)
- **Status**: design approved · not implemented
- **Touches**: `friday_agent/`, `tests/`, `scripts/verify/`, `docs/architecture/`, `CLAUDE.md`, `README.md`

---

## 1. Principle

Friday is a stateless, server-side, general-purpose agent loop. The loop is responsible for two things only: the `tool_use`↔`tool_result` pairing, and handing back the state to persist. How a pending pair is closed (a result, a cancellation, a timeout) and when to retry are the caller's decisions.

## 2. Goals / Non-Goals

### Goals

Seven changes, in delivery order:

| # | Change | Kind | Public contract |
|---|---|---|---|
| 1 | Summary tag extraction fix | bug | none |
| 2 | Turn-ending `Terminal` carries the final state | contract | `Terminal.state` |
| 3 | OpenAI adapter: tool messages before trailing user text | bug | none |
| 4 | Per-turn sections hook | contract | `FridayAgent(turn_sections=)` |
| 5 | Image-bearing tool results | contract | `ToolResult.image` |
| 6 | Deferred tools | contract | `Tool.is_deferred`, `Suspended`, `pending_tool_uses()`, `resume()`, `PendingToolUseError` |
| 7 | Same-prefix compaction | contract | `compact(..., reuse_prefix=)` |

Every change is opt-in or a bug fix: a caller that uses none of the new surfaces sends byte-identical requests and receives the same sentinels as before.

### Non-Goals

- Usage-based proactive compaction, token-usage fields on `LoopState`, and any `TokenUsage` sum normalization.
- Retrying transient LLM errors inside the loop. The loop keeps returning `Terminal(model_error)`; retrying stays the caller's job (e.g. by wrapping the provider).
- A static-section hook (`system_sections`). Static content stays one `system_prompt` string composed by the caller.
- Validating image payloads.
- Everything the scope charter in `00-overview.md` excludes.

## 3. Design

### 3.1 Summary tag extraction fix

**Problem.** `compact_conversation` pairs the first `<summary>` with the first `</summary>` in the response. When the summarizer closes its `<analysis>` block with `</summary>` by mistake, the closing tag precedes the opening one and the extracted summary is an empty string: the whole history is replaced by a summary with no content.

**Design.** A private `_extract_summary(raw_text) -> str | None` in `context/compact.py`:
- find `<summary>`, then search for `</summary>` only after it;
- return the stripped body, or `None` when either tag is missing or the body is empty.

`None` takes the existing fallback (the whole response text).

**Acceptance** (`tests/test_compact.py`)
- a stray `</summary>` before `<summary>` → the real body;
- `<summary></summary>` → fallback;
- a missing closing tag → fallback;
- a well-formed response → the same result as today.

**Docs.** `04-context-compaction.md`.

### 3.2 Turn-ending `Terminal` carries the final state

**Problem.** A tool turn ends with the next `LoopState`, but a text-only turn ends with a bare `Terminal(completed)`. A stateless caller that continues on the next user input has to reassemble the state from the yielded messages.

**Design.**
- `Terminal` gains `state: LoopState | None = None`. Every `Terminal` the loop emits fills it:
  - `completed`: input messages + this turn's assistant message; `todos` unchanged; `turn_count + 1`.
  - `model_error`: the input state, unchanged. The error can only come from the provider call, before any assistant message exists, so there is no progress to add — `step(terminal.state)` repeats the same call.
- No extra `LoopState` is yielded before `Terminal`; callers read `LoopState` as "continue".
- `turn_count + 1` on `completed` makes one rule hold for every sentinel: a turn that produces an assistant message advances `turn_count` by one (`LoopState`, `Suspended`, `Terminal(completed)`); `model_error` does not.
- **Cleanup.** The `model_error` backfill (`yield_missing_tool_result_blocks`) is removed. It always returns an empty list (the error precedes any assistant message), yet `01`/`06` cite it as the pairing guarantee. The `06` row is rewritten to the real guarantee: `run_tools` emits exactly one `tool_result` per executed `tool_use` (errors included), and the only error path fires before an assistant message exists.

After this change every turn-ending sentinel carries the state to persist — `LoopState` itself, `Suspended.state` (§3.6), `Terminal.state`. The caller persists it whatever the reason; to retry, it calls `step(terminal.state)`.

**Acceptance**
- text-only turn: `state.messages` = input + one assistant message; `todos` equal the input; `turn_count` = input + 1;
- the provider raises `LLMError`: `state` is the input state, and `step(terminal.state)` sends the same messages again;
- tool turns are unchanged (existing tests stay green) and existing callers need no change.

**Docs.** `01-core-loop.md`, `06-invariants.md` (row 1), `07-data-models.md`, `CLAUDE.md` (Terminal bullet), `README.md` (Quick Start).

### 3.3 OpenAI adapter: tool messages before trailing user text

**Problem.** Turn-local reminders ride the trailing user message, which after a tool turn is a `tool_result` message. `OpenAIProvider._to_openai_messages` emits a user turn's text before its tool messages, so OpenAI receives `assistant(tool_calls) → user → tool` and rejects the request (tool messages must directly follow `tool_calls`). This already fails whenever todos or memory are active; per-turn sections (§3.4) would make it the common case.

**Design.** In the user branch of `_to_openai_messages`, emit the tool messages first, then the joined text as a user message.

**Acceptance.** A user message `[tool_result, text]` converts to `tool` then `user`; text-only and tool-only user messages are unchanged.

**Docs.** `03-llm-providers.md` (adapter differences table).

### 3.4 Per-turn sections hook (`turn_sections`)

**Problem.** State that changes every turn (the current screen, progress so far) has no official place to go. Put in the system prompt, each change rewrites the whole conversation cache. `run_one_turn(turn_reminders=)` exists, but `FridayAgent.step()` only feeds it the memory index.

**Design.**
- `FridayAgent(..., turn_sections: list[TurnSection] | None = None)`, where `TurnSection = Callable[[LoopState], Awaitable[str]]`.
- Each `step()` awaits the sections in order, passing the input state. Empty strings are dropped. The SDK wraps each non-empty output with `wrap_system_reminder` — the Anthropic adapter's cache-breakpoint skip keys on that prefix, so a caller cannot put a breakpoint on per-turn text by forgetting the tag.
- The outputs join the trailing user message of the API view only (the existing `turn_reminders` path), never `LoopState`.
- Order: todo reminder → memory index → `turn_sections` (list order).
- A section that raises propagates; it is not swallowed.
- The `CLAUDE.md` rule "no engine-level dynamic injection hooks" becomes: `turn_sections` is the single engine-level dynamic hook; static content stays one `system_prompt` string.

**Acceptance** (`tests/test_engine_turn_sections.py`)
- section output appears only in the last user message of the API request and never in the yielded `LoopState.messages`;
- without `turn_sections`, request bytes are unchanged;
- across turns, everything up to `messages[-2]` is byte-identical (cache invariant);
- order, empty-string drop, `<system-reminder>` wrapping, and the state argument.

**Docs.** `01-core-loop.md`, `00-overview.md`, `CLAUDE.md`, `README.md` (extension guide).

### 3.5 Image-bearing tool results

**Problem.** A `tool_result` carries a string only, so a tool cannot return a screenshot, a rendered page, or a chart.

**Design.**
- `ToolResult.image: dict | None = None` — `{"media_type": "image/png", "data": "<base64>"}`.
- `ContentBlock.content` widens to `str | list[dict] | None`. `create_tool_result_message(..., image=None)`: with an image, content is `[{"type": "text", "text": body}, {"type": "image", "source": {"type": "base64", **image}}]`; without one, the same string as today.
- One `ToolResult` → `tool_result` message conversion, `to_tool_result_message(tool_use_id, result)` in `tools/orchestrator.py`, shared by `run_tools` and `resume()` (§3.6) so errors and images behave the same on both paths.
- `normalize_for_api` passes the content through. The Anthropic adapter sends the block array as-is. The OpenAI adapter flattens it: text blocks are joined and each image becomes the marker `[image omitted: not supported by the OpenAI adapter]` — serializing the base64 would cost tokens without the model seeing the image.
- `LoopState` serde round-trips the block array unchanged.
- The image dict is not validated; the tool author owns the payload, and a malformed one surfaces as a provider error.

**Acceptance**
- a result without an image: request bytes unchanged;
- with an image: the Anthropic request carries an `image` block;
- the OpenAI request carries text plus the marker only;
- `to_dict()`/`from_dict()` round-trips equal.

**Docs.** `05-messages.md`, `03-llm-providers.md` (adapter table), `07-data-models.md`, `02-tool-orchestration.md` (`ToolResult`), `README.md` (tools guide).

### 3.6 Deferred tools

**Problem.** Some tool results arrive later and from elsewhere: external execution, human approval, long-running jobs. Today the process must hold the turn open until the result exists. The only workaround is to close the `step()` generator right after the assistant message so `run_tools` never runs, persist a state with an unpaired `tool_use`, and append the result by hand later — behavior the SDK never promised, and every resume or cancel path then has to special-case the unpaired call.

**Design.**
1. `Tool.is_deferred(input: dict) -> bool`, default `False` — an execution-policy method like `is_concurrency_safe`, evaluated the same way: the input is validated against the schema first, and an unknown tool, invalid input, or a raising predicate counts as not deferred. Such a call runs inline and the model gets an immediate error, instead of an external executor receiving a malformed call. Deferred blocks are removed before partitioning, so the concurrency rules apply to the remaining calls unchanged.
2. After the assistant message, `run_one_turn` holds back the deferred `tool_use` blocks, runs the rest exactly as today, and ends with `Suspended(state, pending)` instead of `LoopState`:
   - `state`: input messages + assistant message + results of the tools that ran; `todos` and `turn_count` updated as in any tool turn; serializable as usual.
   - `pending`: the deferred `tool_use` blocks, in `tool_use` order.

   A response without deferred calls behaves exactly as today.
3. Pure state functions in `core/loop.py` — no provider, no tools, so any process can call them:
   - `pending_tool_uses(state) -> list[ContentBlock]`: the `tool_use` blocks of the last assistant message that have no `tool_result` after it. Pending calls are always recomputed from history; `LoopState` gets no new field.
   - `resume(state, results: dict[str, ToolResult]) -> LoopState`: converts each result with the shared conversion (§3.5) and inserts it so that all results for that assistant message sit in `tool_use` order, ahead of any other trailing message; applies `state_effect`s in `tool_use` order; keeps `turn_count`; never calls the model; returns a new state.
     - Partial results are allowed — the rest stay pending. An empty `results` returns an equal state.
     - An id that is not pending (unknown or already answered) raises `ValueError`.

   The next turn runs through `step()`, which stays the single entry point for starting and resuming.
4. `PendingToolUseError(ValueError)` (carrying `.tool_use_ids`) is raised before the provider is called when `step()` (via `run_one_turn`) or `compact()` receives a state with pending calls — including when a user message was appended after them. Today such a state reaches the API, comes back as a 400, and surfaces only as `Terminal(model_error)`.
5. `Suspended` and `PendingToolUseError` live in `core/state.py`. `step()`'s final sentinel becomes `LoopState | Suspended | Terminal`.

**Decided.**
- **Mixed responses.** Non-deferred calls run immediately; only deferred ones wait. Calls in one response have fixed inputs and do not depend on each other's results, so running some first does not change their meaning — and a human approval has no synchronous path to fall back on.
- **New input before resume.** Rejected (item 4). How to close a pending call — cancellation, timeout, a superseding instruction — is the caller's decision; filling it silently would hide lost results. To close one, the caller passes an `is_error` result to `resume`, appends the new user message, and calls `step()`. Tool results are already sent as consecutive user messages, so the shape is valid.
- **`state_effect` on resume.** Applied, the same as for tools run inside `step()`.
- **`compact()` rejects pending state** as well: its summary call would otherwise send the unpaired `tool_use` and fail.

**Acceptance** (`tests/test_deferred_tools.py`)
- a response calling only a deferred tool: the tool does not run, `Suspended` is yielded, and `state` survives a `to_dict()`/`from_dict()` round trip;
- mixed with a normal tool: the normal tool's result is in `Suspended.state`, and `pending` holds only the deferred call;
- `resume` with all results: results sit in `tool_use` order and `step()` continues normally;
- `resume` with some results: fewer calls pending, and `step()` on that state raises;
- a pending state (also with a trailing user message): `step()` raises without calling the provider;
- cancellation (`is_error` result via `resume` → user message → `step()`) completes with one model call;
- `resume` rejects unknown and already-answered ids, and inserts results ahead of a previously appended user message;
- `compact()` on a pending state raises;
- callers without deferred tools never see `Suspended`, and their request bytes are unchanged.

**Docs.** `06-invariants.md` first, in its own commit; then `01-core-loop.md`, `02-tool-orchestration.md`, `07-data-models.md`, `CLAUDE.md` (public API, sentinels, pitfalls), `README.md` (tools guide, distributed resume).

### 3.7 Same-prefix compaction (`reuse_prefix`)

**Problem.** `compact()` calls the summarizer with its own system prompt and `tools=[]`. That prefix differs from the agent's, so the call cannot read the cached history; instead it writes the whole history to the cache at 1.25× — an entry nothing ever reads. With an identical prefix, the history is read at 0.1×.

**Design.**
- `engine.compact(state, *, reuse_prefix: bool = False)` — a per-call option, so a caller can turn it on for proactive compaction and leave it off for overflow recovery.
- On: the summary call sends exactly the system prompt and tool schemas `step()` sends; both come from the same private helpers, so they cannot drift. The compaction prompt (including `compact_instructions`) is the final user message.
- If the response contains a `tool_use` or has no usable `<summary>` (§3.1), call once more with `tools=[]` and the same system prompt; only that retry pays today's price. A `tool_use` from the summarizer never enters state — only the summary text is used — so pairing cannot break.
- `compact_conversation()` gains optional `system_prompt` and `tools` parameters (defaults: today's summarizer prompt and `[]`) and owns the retry.
- Off (the default): unchanged.
- Documented limits: the summary call carries the agent's system and tool tokens, so during overflow recovery it can overflow itself. With extended thinking enabled in the agent config, the summary call (thinking off) cannot reuse the message cache, because thinking settings are part of the cached prefix.

**Acceptance**
- with the option on, the summary call's system prompt and tools equal those of a `step()` call on the same state;
- a response containing `tool_use` triggers one `tools=[]` retry, whose result is used;
- with the option off, behavior is unchanged.

**Docs.** `04-context-compaction.md`, `01-core-loop.md` (`compact` API), `CLAUDE.md` (public API).

## 4. Public API After This Work

| Symbol | Change |
|---|---|
| `FridayAgent(..., turn_sections=None)` | new parameter |
| `engine.step(state)` | final sentinel is `LoopState \| Suspended \| Terminal` |
| `engine.compact(state, *, reuse_prefix=False)` | new option; raises `PendingToolUseError` on a pending state |
| `Terminal.state` | new field |
| `Suspended(state, pending)` | new sentinel (`core/state.py`) |
| `PendingToolUseError(tool_use_ids)` | new exception (`core/state.py`) |
| `pending_tool_uses(state)`, `resume(state, results)` | new functions (`core/loop.py`) |
| `Tool.is_deferred(input)` | new policy method, default `False` |
| `ToolResult.image` | new field |
| `ContentBlock.content` | `str \| list[dict] \| None` |
| `create_tool_result_message(..., image=None)` | new parameter |
| `to_tool_result_message(tool_use_id, result)` | new function (`tools/orchestrator.py`) |
| `yield_missing_tool_result_blocks` | removed (dead code) |

## 5. Delivery

- Branch `refactor/loop-contracts`: one commit per change, in the order of §2, each with its code, tests, and docs, all in English. Change 6 is preceded by its own `06-invariants.md` commit.
- `python -m pytest` stays green after every commit.
- Real-API checks in `scripts/verify/` cover what fake providers cannot prove. They run once at the end, after the user's go-ahead (token cost):
  - image delivery — Anthropic sees the image, OpenAI accepts the marker (`verify_image.py`, new);
  - the deferred flow and cancellation, accepted by both vendors (`verify_deferred.py`, new);
  - the OpenAI ordering fix (existing `verify_todo.py`, run with an OpenAI model);
  - turn sections keep the cache hitting, and `reuse_prefix` compaction reads the cache (`verify_cache.py`, extended).
- This spec and the implementation plan are deleted in the branch's last commit.
- No push, PR, or version bump unless asked.

## 6. Decision Log

| # | Decision | Why |
|---|---|---|
| D1 | `Terminal(completed).state.turn_count` = input + 1 | One "+1 per turn that produced an assistant message" rule across all sentinels |
| D2 | Remove `yield_missing_tool_result_blocks` | Provably a no-op, while the docs cited it as a guarantee |
| D3 | OpenAI: tool messages before trailing user text | Existing 400 with todos or memory; turn sections would make it common |
| D4 | The SDK wraps turn sections in `<system-reminder>` | A forgotten tag puts a cache breakpoint on per-turn text |
| D5 | Sections receive the input `LoopState` | History-derived sections need no side channel |
| D6 | No `system_sections` | No consumer; static content is the caller's `system_prompt` |
| D7 | `resume` / `pending_tool_uses` are pure functions | Attaching a result needs no provider or key, so any process can do it; `step()` stays the only way to run a turn |
| D8 | Mixed responses run non-deferred calls immediately | Inputs are fixed per response; a human approval has no synchronous path |
| D9 | New input before resume is rejected | Closing a pending call is the caller's decision; silent filling hides lost results |
| D10 | `resume` applies `state_effect` | `ToolResult` means the same on both paths |
| D11 | `resume` puts results ahead of other trailing messages | Tool results must precede text in a user turn; a rejected state recovers with a single `resume` |
| D12 | `compact()` also rejects a pending state | Its summary call would send the unpaired `tool_use` |
| D13 | `PendingToolUseError` subclasses `ValueError` | A caller-side invalid state, never a `model_error` |
| D14 | `reuse_prefix` is a per-call option | Proactive and overflow compaction want different trade-offs |
| D15 | The image dict is not validated | The tool author owns the payload (YAGNI) |
