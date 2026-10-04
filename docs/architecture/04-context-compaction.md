# 04. Context Compaction

## ① Purpose

On context overflow (token limit exceeded), reduces the entire conversation to a single summary message in a **caller-driven** way.
`step()` does not recover from overflow itself; it raises `ContextOverflowError` and propagates it to the caller.
When the caller calls `engine.compact(state)`, the conversation is summarized using the LLM and a reduced `LoopState` is returned.
The loop then recovers when the caller retries `step()`.

---

## ② Owned Files

| Path | Responsibility | Key Symbols |
|---|---|---|
| `friday_agent/context/compact.py` | Conversation summary generation · summary message construction | `compact_conversation()`, `create_compact_summary_message()`, `build_compact_prompt()`, `COMPACT_PROMPT`, `MAX_OUTPUT_TOKENS_FOR_SUMMARY` |
| `friday_agent/core/engine.py` | compact entry point | `FridayAgent.compact()` |

---

## ③ Core Behavior / Flow

```
step() → ContextOverflowError raise
   └─ caller calls engine.compact(state)
         ├─ normalize_for_api(state.messages)          # convert to a list of API-format dicts
         └─ compact_conversation(provider, messages, extra_instructions, system_prompt, tools)
               #   default: summarizer system = SUMMARIZER_SYSTEM_PROMPT, tools=[]
               #   reuse_prefix=True: system + tools byte-identical to step()'s
               ├─ append build_compact_prompt(extra_instructions) as the last user message
               ├─ provider.complete(tools=[], config=config_type(max_tokens=20000))
               │     # tools=[] : tool calls strictly forbidden during summarization
               │     # reuse_prefix: complete(tools=agent tools); a tool_use or no <summary> → retry once with tools=[]
               └─ extract the <summary> body (_extract_summary)
                     · </summary> is searched only after <summary>
                     · <analysis> block is discarded
                     · a missing pair or an empty body falls back to the full text
         → create_compact_summary_message(summary)     # user message with is_compact_summary=True
         → return LoopState(messages=[summary], turn_count preserved)
   └─ caller retries step()
```

### Items Preserved by COMPACT_PROMPT

`_COMPACT_PROMPT_HEAD` in `context/compact.py:39` instructs the LLM to preserve the following 9 items in the summary:

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

**The no-tools guard appears only once** (the first `CRITICAL:` line). By default `compact_conversation` calls with `tools=[]`, and both adapters omit the `tools` field entirely when the list is empty (`anthropic_provider.py:145-146`, `openai_provider.py:168-169`), so the model cannot produce a `tool_use` block in the first place (with `reuse_prefix=True` the agent's tools are sent, and a `tool_use` reply is retried once with `tools=[]`). The one remaining occurrence is belt-and-suspenders for third-party `LLMProvider` implementations that ignore the `tools` argument — the repetitions were dead letters and were removed. Regression guard: `tests/test_compact.py::test_no_tools_guard_appears_exactly_once`.

### Domain Instruction Injection Slot (opt-in)

By default the summary call does not receive the caller's `system_prompt` (it uses the dedicated `SUMMARIZER_SYSTEM_PROMPT`; `reuse_prefix=True` sends the agent's own prompt, but only to share the cache prefix). So the **only channel** for specifying "what must this domain's summary keep" is `FridayAgent(..., compact_instructions="...")`. The value is passed through `engine.compact()` → `compact_conversation(extra_instructions=...)` → `build_compact_prompt()`.

```
<head: CRITICAL guard (no-tools once · no-questions) + analysis instructions + 9-section spec>

Domain-specific requirements for this summary
(these take precedence over the generic sections above):
<compact_instructions body>                              ← injection slot

<tail: Output format + REMINDER (output format contract)>
```

- **Position is the contract** — the slot sits after section 9 and before `Output format`. The trailing `REMINDER` carries the `<analysis>`/`<summary>` **output format contract**, and recency is what drives compliance with it. An untagged response falls back to the full text, mixing the scratchpad into the summary, so the injected block must not push this position out.
- **precedence header** — thanks to the "take precedence over the generic sections above" wording, both adding sections (write a section 10) and redefining existing ones (handle section 3 like this) are covered by a single slot. Hence there is **no full-replacement option** for the prompt — allowing replacement would leave the vendored copy in a forked state that cannot receive upstream prompt improvements.
- **Default is no-op** — with `compact_instructions=""` (default), the output of `build_compact_prompt()` is **byte-for-byte identical** to `COMPACT_PROMPT`. A whitespace-only string is also a no-op.
- Being an engine-local setting, the `LoopState` serialization surface is unchanged — on distributed resume it is re-injected container-locally together with `provider`·`system_prompt`.

### Same-Prefix Compaction (`reuse_prefix`, opt-in)

By default the summary call's prefix (summarizer system prompt, no tools) differs from the agent's, so it cannot read the cached conversation — instead it writes the whole history to the cache at 1.25×, an entry nothing ever reads. `engine.compact(state, reuse_prefix=True)` sends the summary call with exactly the system prompt and tool schemas `step()` sends (both come from the same private helpers, so they cannot drift); the compaction prompt is the final user message. The history is then read at ~0.1×.

- **Retry**: if the reply contains a `tool_use` or has no usable `<summary>`, the call is retried once with `tools=[]` (same system prompt) — only that retry pays the full price. A `tool_use` from the summarizer never enters state; only the summary text is used.
- **Per call**: turn it on for proactive compaction. During overflow recovery, the agent's system and tool tokens can push the summary call itself over the window.
- **Thinking**: thinking settings are part of the cached prefix. With extended thinking enabled in the agent config, the summary call (thinking off) cannot reuse the message cache.
- **Default off**: `compact(state)` is unchanged.

---

## ④ Public API

### `engine.compact(state) -> LoopState`

```python
async def compact(self, state: LoopState, *, reuse_prefix: bool = False) -> LoopState:
```

- `reuse_prefix=True`: same-prefix summary call (see above)

- Argument: the current `LoopState` (including messages)
- Returns: a new `LoopState` holding a single summary message (`messages=[summary_message]`) and the preserved `turn_count`
- The caller passes the returned reduced state straight to `step()` to retry
- Domain summary instructions are set once via the constructor `FridayAgent(..., compact_instructions=...)`, not as a call argument
- Raises `PendingToolUseError` when the state still has unanswered `tool_use` blocks (a suspended turn) — attach them with `resume()` first; the summary call would otherwise send the unpaired `tool_use`.

### `compact_conversation()` — for direct use

```python
async def compact_conversation(
    *,
    provider: LLMProvider,
    messages: list[dict],
    extra_instructions: str = "",
    system_prompt: str = SUMMARIZER_SYSTEM_PROMPT,
    tools: list[dict] | None = None,
) -> str:
```

- Default `tools=[]` (summarizer prompt). With `tools`, a tool_use or no `<summary>` triggers one `tools=[]` retry. Uses `config_type(max_tokens=20000)`.
- Return value: the extracted summary text string (after tag removal)

### `build_compact_prompt()` — prompt rendering

```python
def build_compact_prompt(extra_instructions: str = "") -> str:
```

- If the argument is empty (or whitespace-only), returns a string identical to `COMPACT_PROMPT`.
- The `COMPACT_PROMPT` constant itself is defined as the result of `build_compact_prompt()`, so the two cannot diverge.

---

## ⑤ Dependencies

| Dependency Module | Symbols Used | Purpose |
|---|---|---|
| `friday_agent/api/provider.py` | `LLMProvider.complete()`, `LLMProvider.config_type` | Summary LLM call |
| `friday_agent/messages/types.py` | `create_user_message()` | Summary message creation |
| `friday_agent/messages/normalize.py` | `normalize_for_api()` | Engine-side conversion before the call |

See [01-core-loop](01-core-loop.md) for the full context of the call flow.

---

## ⑥ Maintenance Notes

- **No tool calls reach state**: on the default path, the `complete()` call inside `compact_conversation()` uses `tools=[]` — the enforcement mechanism (the prompt's no-tools line only helps third-party providers). With `reuse_prefix`, the first call carries the agent's tools; a `tool_use` reply is discarded and retried with `tools=[]`. Either way only the summary text is used, so `tool_use`↔`tool_result` pairing cannot break.
- **`is_compact_summary=True` flag**: the user message produced by `create_compact_summary_message()` is marked `is_compact_summary=True` (`context/compact.py:124`). This flag ties into the message type classification in [05-messages](05-messages.md); removing or omitting it can cause message filtering logic to misclassify the summary message as a regular user message.
- **`turn_count` preserved**: `engine.compact()` returns `LoopState(messages=[summary_message], turn_count=state.turn_count)` (`core/engine.py:221`). `turn_count` must not be reset after reduction so that observability metrics are maintained.
- **`<summary>` extraction and fallback**: `_extract_summary()` (`context/compact.py`) finds `<summary>` and searches for `</summary>` only after it — a summarizer that mistakenly closes its `<analysis>` with `</summary>` would otherwise produce an empty summary and wipe the history. A missing pair or an empty body returns `None`, and the full response text is used instead. This is defensive code so the loop never halts on a format slip; lower format compliance degrades summary quality, so take care when modifying the prompt.
- **Separate summarizer system (default)**: by default the summary call runs under the dedicated `SUMMARIZER_SYSTEM_PROMPT`; `engine.compact()` passes the agent's own system prompt only with `reuse_prefix=True`.
- **Continuation framing**: `create_compact_summary_message()` wraps the summary in a "continuing the previous conversation" preamble + a "resume directly, no further questions" directive. This is for smooth resumption after compaction; changing the body affects resume behavior.

---

## ⑦ Design Rationale (Why)

Unlike the **internal Auto/Reactive Compact** approach of the original guideline (04) (detecting and recovering from overflow inside the loop), this implementation adopts a **caller-driven (caller-owned)** approach:

- On overflow, `step()` raises `ContextOverflowError` and immediately propagates it to the caller.
- The caller recovers by explicitly calling `engine.compact(state)`, then retries `step()`.

This separation provides:
1. **Clear library boundary**: `step()` is responsible only for single-turn execution; the recovery policy is decided by the caller.
2. **Distributed-resume friendliness**: `ContextOverflowError` is serializable, making it easy for a distributed orchestrator to save and restore state.
3. **Testability**: the compact path can be tested independently on the caller side.
