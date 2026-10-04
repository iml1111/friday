# 04. Context Compaction

## ① Purpose

Reduces the entire conversation to a single summary message in a **caller-driven** way.
When the caller calls `engine.compact(state)`, the conversation is summarized using the LLM and a reduced `LoopState` is returned.
`step()` does not recover from overflow itself; it raises `ContextOverflowError` and propagates it to the caller.
The summary call shares `step()`'s exact prefix (system prompt, tools, config) so it reads the conversation from the prompt cache — which also means a state that already overflowed `step()` overflows the summary call too. Compact **proactively**, before the window fills; after an overflow, the caller first trims the state itself (e.g. drops the oldest turns, keeping `tool_use`↔`tool_result` pairs), then compacts and retries `step()`.

---

## ② Owned Files

| Path | Responsibility | Key Symbols |
|---|---|---|
| `friday_agent/api/prompts.py` | Compaction prompt text · summary message text, each with a format helper right below it | `COMPACT_PROMPT` + `format_compact_prompt()`, `COMPACT_SUMMARY_MESSAGE` + `format_compact_summary_message()` |
| `friday_agent/core/engine.py` | compact entry point and the summary call (it owns the prefix the call shares with `step()`) | `FridayAgent.compact()`, `FridayAgent._extract_summary()` |

---

## ③ Core Behavior / Flow

```
caller decides to compact (proactively, or after trimming an overflowed state)
   └─ engine.compact(state)
         ├─ normalize_for_api(state.messages)          # convert to a list of API-format dicts
         ├─ append format_compact_prompt(compact_instructions) as the last user message
         ├─ provider.complete(system + tools = step()'s own, config = copy of the agent's, max_tokens ≥ 20000)
         │     # a tool_use or no <summary> → retry once with tools=[]
         ├─ extract the <summary> body (_extract_summary)
         │     · </summary> is searched only after <summary>
         │     · <analysis> block is discarded
         │     · a missing pair or an empty body falls back to the full text
         ├─ format_compact_summary_message(summary)    # user message with is_compact_summary=True
         └─ return LoopState(messages=[summary], turn_count preserved)
   └─ caller retries step()
```

### Items Preserved by COMPACT_PROMPT

`COMPACT_PROMPT` in `api/prompts.py:95` instructs the LLM to preserve the following 9 items in the summary:

1. Primary Request and Intent
2. Key Technical Concepts
3. Files and Code Sections (including actual code snippets)
4. Errors and Fixes
5. Problem Solving Progress
6. All User Messages (excluding tool results)
7. Pending Tasks
8. Current Work (precise description of the most recent work)
9. Optional Next Step (including direct quotes)

The output format is `<analysis>scratchpad</analysis><summary>summary body</summary>`, and the `<analysis>` block is discarded on extraction. Beyond the 9 items, `COMPACT_PROMPT` includes systematic analysis instructions and a format trailer to raise `<summary>` format compliance (based on the original `services/compact/prompt.ts`, with development-specific wording generalized).

**The no-tools guard appears only once** (the first `CRITICAL:` line). The summary call carries the agent's tools (they are part of the shared prefix), so this line is the request; a reply that calls a tool anyway is retried once with `tools=[]`, and both adapters omit the `tools` field entirely when the list is empty (`anthropic_provider.py:145-146`, `openai_provider.py:141-142`), so the retry cannot produce a `tool_use` block. Repetitions of the guard were dead letters and were removed. Regression guard: `tests/test_compact.py::test_no_tools_guard_appears_exactly_once`.

### Domain Instruction Injection Slot (opt-in)

The summary call does carry the caller's `system_prompt`, but only as the shared cache prefix — the compaction prompt that follows it governs the reply. The **channel** for specifying "what must this domain's summary keep" is `FridayAgent(..., compact_instructions="...")`. `engine.compact()` passes it to `format_compact_prompt()`, which places it in `COMPACT_PROMPT`'s `{domain_requirements}` slot under a precedence header.

```
<head: CRITICAL guard (no-tools once · no-questions) + analysis instructions + 9-section spec>

Domain-specific requirements for this summary
(these take precedence over the generic sections above):
<compact_instructions body>                              ← injection slot

<tail: Output format + REMINDER (output format contract)>
```

- **Position is the contract** — the slot sits after section 9 and before `Output format`. The trailing `REMINDER` carries the `<analysis>`/`<summary>` **output format contract**, and recency is what drives compliance with it. An untagged response falls back to the full text, mixing the scratchpad into the summary, so the injected block must not push this position out.
- **precedence header** — thanks to the "take precedence over the generic sections above" wording, both adding sections (write a section 10) and redefining existing ones (handle section 3 like this) are covered by a single slot. Hence there is **no full-replacement option** for the prompt — allowing replacement would leave the vendored copy in a forked state that cannot receive upstream prompt improvements.
- **Default is no-op** — with `compact_instructions=""` (default), the slot is filled with `""` and the prompt is the base prompt, **byte for byte**. A whitespace-only string is also a no-op.
- Being an engine-local setting, the `LoopState` serialization surface is unchanged — on distributed resume it is re-injected container-locally together with `provider`·`system_prompt`.

### Shared Prefix

A summary call with its own prefix would miss the cached conversation and pay full price for the whole history. `engine.compact(state)` therefore sends the summary call with exactly the system prompt and tool schemas `step()` sends (both come from the same private helpers, so they cannot drift), under the agent's own config; the compaction prompt is the final user message. The history is then read at ~0.1×.

- **Config**: a copy of the agent's config — settings the message cache keys on (e.g. extended thinking) match `step()`'s; only `max_tokens` is raised to at least 20,000 (output length does not affect the cache). The agent's config object is never mutated.
- **Retry**: if the reply contains a `tool_use` or has no usable `<summary>`, the call is retried once with `tools=[]` (same system prompt and config) — only that retry pays the full price. A `tool_use` from the summarizer never enters state; only the summary text is used.
- **Overflow**: the summary call is `step()`'s request plus the compaction prompt, so it overflows wherever `step()` did. Trimming an overflowed state is the caller's job (see ①).

---

## ④ Public API

### `engine.compact(state) -> LoopState`

```python
async def compact(self, state: LoopState) -> LoopState:
```

- Argument: the current `LoopState` (including messages)
- Returns: a new `LoopState` holding a single summary message (`messages=[summary_message]`) and the preserved `turn_count`
- The caller passes the returned reduced state straight to `step()` to retry
- Domain summary instructions are set once via the constructor `FridayAgent(..., compact_instructions=...)`, not as a call argument
- Raises `PendingToolUseError` when the state still has unanswered `tool_use` blocks (a suspended turn) — attach them with `resume()` first; the summary call would otherwise send the unpaired `tool_use`.
- Propagates `ContextOverflowError` from the summary call (a state that overflowed `step()` overflows here too — trim it first).

---

## ⑤ Dependencies

| Dependency Module | Symbols Used | Purpose |
|---|---|---|
| `friday_agent/api/provider.py` | `LLMProvider.complete()`, `LLMConfig` | Summary LLM call |
| `friday_agent/messages/types.py` | `create_user_message()` | Summary message creation |
| `friday_agent/api/prompts.py` | `format_compact_prompt()`, `format_compact_summary_message()`, `assemble_system_prompt()` | Prompt texts · the shared system prompt |
| `friday_agent/messages/normalize.py` | `normalize_for_api()` | Engine-side conversion before the call |

See [01-core-loop](01-core-loop.md) for the full context of the call flow.

---

## ⑥ Maintenance Notes

- **No tool calls reach state**: the first summary call carries the agent's tools; a `tool_use` reply is discarded and retried with `tools=[]`. Only the summary text is used, so `tool_use`↔`tool_result` pairing cannot break.
- **`is_compact_summary=True` flag**: the summary user message built in `engine.compact()` is marked `is_compact_summary=True` (`core/engine.py:231`). This flag ties into the message type classification in [05-messages](05-messages.md); removing or omitting it can cause message filtering logic to misclassify the summary message as a regular user message.
- **`turn_count` preserved**: `engine.compact()` returns `LoopState(messages=[summary], turn_count=state.turn_count, todos=state.todos)` (`core/engine.py:233`). `turn_count` must not be reset after reduction so that observability metrics are maintained.
- **`<summary>` extraction and fallback**: `FridayAgent._extract_summary()` (`core/engine.py`) finds `<summary>` and searches for `</summary>` only after it — a summarizer that mistakenly closes its `<analysis>` with `</summary>` would otherwise produce an empty summary and wipe the history. A missing pair or an empty body returns `None`, and the full response text is used instead. This is defensive code so the loop never halts on a format slip; lower format compliance degrades summary quality, so take care when modifying the prompt.
- **Shared prefix**: `engine.compact()` builds the summary call's system prompt and tools with the same helpers `step()` uses (`_effective_system_prompt()`, `_tool_schemas()`) — a change to one path that skips the other silently loses the cache read.
- **Continuation framing**: `COMPACT_SUMMARY_MESSAGE` wraps the summary in a "continuing the previous conversation" preamble + a "resume directly, no further questions" directive. This is for smooth resumption after compaction; changing the body affects resume behavior.

---

## ⑦ Design Rationale (Why)

Unlike the **internal Auto/Reactive Compact** approach of the original guideline (04) (detecting and recovering from overflow inside the loop), this implementation adopts a **caller-driven (caller-owned)** approach:

- On overflow, `step()` raises `ContextOverflowError` and immediately propagates it to the caller.
- The caller recovers by trimming the state, explicitly calling `engine.compact(state)`, then retrying `step()` — or compacts proactively before the window fills.

This separation provides:
1. **Clear library boundary**: `step()` is responsible only for single-turn execution; the recovery policy is decided by the caller.
2. **Distributed-resume friendliness**: `ContextOverflowError` is serializable, making it easy for a distributed orchestrator to save and restore state.
3. **Testability**: the compact path can be tested independently on the caller side.
