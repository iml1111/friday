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
| `friday_agent/context/compact.py` | Conversation summary generation · summary message construction | `compact_conversation()`, `create_compact_summary_message()`, `COMPACT_PROMPT`, `MAX_OUTPUT_TOKENS_FOR_SUMMARY` |
| `friday_agent/core/engine.py` | compact entry point | `QueryEngine.compact()` |

---

## ③ Core Behavior / Flow

```
step() → ContextOverflowError raise
   └─ caller calls engine.compact(state)
         ├─ normalize_for_api(state.messages)          # convert to a list of API-format dicts
         └─ compact_conversation(provider, messages)   # summarizer system = dedicated SUMMARIZER_SYSTEM_PROMPT (caller role not passed)
               ├─ append COMPACT_PROMPT to the end of messages as the last user message
               ├─ provider.complete(tools=[], config=config_type(max_tokens=20000))
               │     # tools=[] : tool calls strictly forbidden during summarization
               └─ extract <summary>...</summary> from the response text
                     · <analysis> block is discarded
                     · if there is no <summary> tag, the full text is used as a fallback
         → create_compact_summary_message(summary)     # user message with is_compact_summary=True
         → return LoopState(messages=[summary], turn_count preserved)
   └─ caller retries step()
```

### Items Preserved by COMPACT_PROMPT

`COMPACT_PROMPT` in `context/compact.py:22` instructs the LLM to preserve the following 9 items in the summary:

1. Primary Request and Intent
2. Key Technical Concepts
3. Files and Code Sections (including actual code snippets)
4. Errors and Fixes
5. Problem Solving Progress
6. All User Messages (excluding tool results)
7. Pending Tasks
8. Current Work (precise description of the most recent work)
9. Optional Next Step (including direct quotes)

The output format is `<analysis>scratchpad</analysis><summary>summary body</summary>`, and the `<analysis>` block is discarded on extraction. Beyond the 9 items, the reinforced `COMPACT_PROMPT` includes a mandatory no-tools preamble · systematic analysis instructions · a trailer to suppress tool calls and raise `<summary>` format compliance (based on the original `services/compact/prompt.ts`, with development-specific wording generalized).

---

## ④ Public API

### `engine.compact(state) -> LoopState`

```python
# core/engine.py:116
async def compact(self, state: LoopState) -> LoopState:
```

- Argument: the current `LoopState` (including messages)
- Returns: a new `LoopState` holding a single summary message (`messages=[summary_message]`) and the preserved `turn_count`
- The caller passes the returned reduced state straight to `step()` to retry

### `compact_conversation()` — for direct use

```python
# context/compact.py:80
async def compact_conversation(
    *,
    provider: LLMProvider,
    messages: list[dict],
) -> str:
```

- Fixed to `tools=[]`. Uses `config_type(max_tokens=20000)`.
- Return value: the extracted summary text string (after tag removal)

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

- **`tools=[]` required**: the `provider.complete()` call inside `compact_conversation()` must use `tools=[]` (`context/compact.py:114`). If tool calls were allowed during summarization, tool_use↔tool_result pairing integrity could break.
- **`is_compact_summary=True` flag**: the user message produced by `create_compact_summary_message()` is marked `is_compact_summary=True` (`context/compact.py:70-73`). This flag ties into the message type classification in [05-messages](05-messages.md); removing or omitting it can cause message filtering logic to misclassify the summary message as a regular user message.
- **`turn_count` preserved**: `engine.compact()` returns `LoopState(messages=[summary_message], turn_count=state.turn_count)` (`core/engine.py:132`). `turn_count` must not be reset after reduction so that observability metrics are maintained.
- **`<summary>` tag fallback**: if there is no `<summary>` tag, the full response text is used as-is (`context/compact.py:128-129`). This is defensive code designed so the loop does not halt even if the LLM breaks the format. Lower format compliance degrades summary quality, so take care when modifying the prompt.
- **Separate summarizer system**: the system for the summary call is the dedicated `SUMMARIZER_SYSTEM_PROMPT` (`context/compact.py`). `engine.compact()` does not pass the caller's domain role to the summary call (summarization is, at its core, a "summarize" task).
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
