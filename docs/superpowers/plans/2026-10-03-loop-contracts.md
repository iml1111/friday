# Loop Contract Extensions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add seven loop-contract changes to `friday_agent` — a summary-parsing fix, `Terminal.state`, an OpenAI ordering fix, `turn_sections`, image tool results, deferred tools (`Suspended`/`resume`), and same-prefix compaction — each opt-in or a bug fix.

**Architecture:** All changes stay inside the existing layers. The loop (`core/loop.py`) owns pairing and the sentinel it yields (`LoopState` | `Suspended` | `Terminal`, each carrying the state to persist). The engine (`core/engine.py`) owns prompt and section assembly. Pure state functions (`pending_tool_uses`, `resume`) live beside the loop. Adapters own vendor wire rules.

**Tech Stack:** Python 3.11+, pytest (asyncio_mode=auto), pydantic v2, `anthropic` + `openai` SDKs (real-API scripts only).

**Spec:** `docs/superpowers/specs/2026-10-03-loop-contracts-design.md`

## Global Constraints

- Work on branch `refactor/loop-contracts` only. Before every commit, `git rev-parse --abbrev-ref HEAD` must print `refactor/loop-contracts`.
- Commit with explicit paths — `git add <paths>` then `git commit -F - -- <paths>` — and check `git show --stat HEAD` right after. Never `git add -A`.
- Every commit message ends with exactly these two lines:
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
  `Claude-Session: https://claude.ai/code/session_01JRQLSczEWcmgFp8vJsUs9q`
- English only for code, comments, docs, and commit messages.
- Never mention where a change came from — no provenance notes, no other project's name, and no "copy"/"ported"/"downstream" wording in any file or commit message. Describe each change on its own merits.
- A caller that uses none of the new surfaces must send byte-identical requests and receive the same sentinels as before.
- `python -m pytest` needs no API key and must stay green after every task. The baseline is 253 passed.
- Docs ship in the same commit as the code they describe: `docs/architecture/`, `CLAUDE.md`, and `README.md`.
- No push, no PR, no version bump.

## Review Focus

1. **A deferred tool called with input that fails schema validation** — expected: the call is not deferred; it runs inline (`call()`), the model gets an immediate error result, and no `Suspended` is yielded. Test: Task 8 `test_invalid_input_to_deferred_tool_runs_inline_with_error`.
2. **`compact(reuse_prefix=True)` with memory mounted** — expected: the system prompt is still byte-identical to `step()`'s, since `MEMORY_INSTRUCTIONS` is part of the prefix. Test: Task 9 `test_reuse_prefix_matches_step_with_memory_mounted`.
3. **OpenAI: one user turn with several `tool_result`s followed by text** — expected: all tool messages in order, then the user text. Test: Task 3 `test_to_openai_messages_single_user_turn_with_many_results_and_text`.
4. **A deferred call next to an unknown tool in one response** — expected: the unknown tool's error result comes immediately and only the deferred call is pending. Test: Task 8 `test_unknown_tool_beside_deferred_errors_immediately`.
5. **`turn_sections` during compaction** — expected: sections are never rendered and never reach either summary call. Test: Task 9 `test_compact_never_renders_turn_sections`.

---

## File Structure

| File | Change | Responsibility after this work |
|---|---|---|
| `friday_agent/context/compact.py` | modify (T1, T9) | Summary prompt, `_extract_summary`, `compact_conversation(system_prompt=, tools=)` with one no-tools retry |
| `friday_agent/core/state.py` | modify (T2, T7, T8) | `Terminal(state=)`, `LoopState`, `Suspended`, `PendingToolUseError` |
| `friday_agent/core/loop.py` | modify (T2, T7, T8) | `run_one_turn` (guard, deferred split, sentinels), `pending_tool_uses`, `resume` |
| `friday_agent/core/engine.py` | modify (T4, T7, T8, T9) | `turn_sections`, pending guard, `compact(reuse_prefix=)`, shared prefix helpers |
| `friday_agent/api/openai_provider.py` | modify (T3, T5) | Tool messages before text; `_flatten_tool_result_content` |
| `friday_agent/tools/base.py` | modify (T5, T8) | `ToolResult.image`, `Tool.is_deferred` |
| `friday_agent/tools/orchestrator.py` | modify (T5, T8) | `to_tool_result_message`, `is_deferred_call`, shared input validation |
| `friday_agent/messages/types.py` | modify (T5) | `ContentBlock.content: str \| list[dict] \| None`, `create_tool_result_message(image=)` |
| `friday_agent/messages/normalize.py` | modify (T5) | Comment only: tool_result content passes through |
| `tests/_drive.py` | modify (T8) | Test driver understands `Suspended` |
| `tests/test_compact.py` | modify (T1) | Extraction cases |
| `tests/test_terminal_state.py` | create (T2) | `Terminal.state` |
| `tests/test_loop_termination.py` | modify (T2) | Rename the backfill-named test |
| `tests/test_openai_provider.py` | modify (T3) | Ordering cases |
| `tests/test_engine_turn_sections.py` | create (T4) | `turn_sections` |
| `tests/test_tool_result_image.py` | create (T5) | Image results |
| `tests/test_pending_guard.py` | create (T7) | `pending_tool_uses`, `PendingToolUseError` |
| `tests/test_deferred_tools.py` | create (T8) | `is_deferred`, `Suspended`, `resume` |
| `tests/test_compact_reuse_prefix.py` | create (T9) | `reuse_prefix` |
| `scripts/verify/verify_image.py` | create (T10) | Real-API image check |
| `scripts/verify/verify_deferred.py` | create (T10) | Real-API deferred and cancel check |
| `scripts/verify/verify_cache.py` | modify (T10) | Adds turn sections and a `reuse_prefix` compaction read |
| `docs/architecture/0*.md`, `CLAUDE.md`, `README.md` | modify (per task) | Contract docs |

---

### Task 1: Summary tag extraction fix

**Files:**
- Modify: `friday_agent/context/compact.py:129-174`
- Modify: `docs/architecture/04-context-compaction.md`
- Test: `tests/test_compact.py`

**Interfaces:**
- Produces: `_extract_summary(raw_text: str) -> str | None` (module-private; Task 9 reuses it).

- [ ] **Step 1: Write the failing tests** — append to `tests/test_compact.py`:

```python
# ---------------------------------------------------------------------------
# <summary> extraction: the closing tag is searched only after the opening tag
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_compact_conversation_ignores_stray_closing_tag_before_summary():
    """An <analysis> block closed with </summary> must not empty the summary."""
    raw = "<analysis>notes</summary>\n<summary>REAL BODY</summary>"
    provider = FakeLLMProvider(responses=[_summary_response(raw)])

    result = await compact_conversation(provider=provider, messages=[{"role": "user", "content": "x"}])

    assert result == "REAL BODY"


@pytest.mark.asyncio
async def test_compact_conversation_empty_summary_falls_back_to_full_text():
    raw = "<analysis>notes</analysis><summary>  </summary>"
    provider = FakeLLMProvider(responses=[_summary_response(raw)])

    result = await compact_conversation(provider=provider, messages=[{"role": "user", "content": "x"}])

    assert result == raw


@pytest.mark.asyncio
async def test_compact_conversation_unclosed_summary_falls_back_to_full_text():
    raw = "<analysis>notes</analysis><summary>cut off mid-sentence"
    provider = FakeLLMProvider(responses=[_summary_response(raw)])

    result = await compact_conversation(provider=provider, messages=[{"role": "user", "content": "x"}])

    assert result == raw
```

- [ ] **Step 2: Run them and confirm the first two fail**

Run: `python -m pytest tests/test_compact.py -k "stray or empty_summary or unclosed" -v`
Expected: `stray` FAILS (`'' == 'REAL BODY'`), `empty_summary` FAILS (`'' == raw`), `unclosed` passes (already a fallback).

- [ ] **Step 3: Implement.** In `friday_agent/context/compact.py`, replace the tail of `compact_conversation` (from the `if "<summary>" in raw_text ...` block to the end of the file):

```python
    summary = _extract_summary(raw_text)
    if summary is not None:
        return summary

    # No usable <summary> pair — return the full response as a best-effort fallback.
    return raw_text.strip()


def _extract_summary(raw_text: str) -> str | None:
    """Return the text inside ``<summary>``, or None if there is no usable pair.

    The closing tag is searched for only *after* the opening tag: a summarizer
    that closes its ``<analysis>`` block with ``</summary>`` by mistake puts a
    closing tag ahead of the real opening one, and pairing the first of each
    yields an empty summary. An empty body is reported as a miss as well, so the
    caller falls back instead of replacing the history with nothing.
    """
    start = raw_text.find("<summary>")
    if start == -1:
        return None
    start += len("<summary>")
    end = raw_text.find("</summary>", start)
    if end == -1:
        return None
    return raw_text[start:end].strip() or None
```

In the `compact_conversation` docstring, replace `If no tags are present the entire response text is returned as a graceful fallback.` with `Without a usable <summary> pair (see _extract_summary) the entire response text is returned as a graceful fallback.`

- [ ] **Step 4: Run the compact tests**

Run: `python -m pytest tests/test_compact.py -v`
Expected: all PASS.

- [ ] **Step 5: Update `docs/architecture/04-context-compaction.md`**
  - In ③'s flow block, replace the three lines

    ```
                   └─ extract <summary>...</summary> from the response text
                         · <analysis> block is discarded
                         · if there is no <summary> tag, the full text is used as a fallback
    ```

    with

    ```
                   └─ extract the <summary> body (_extract_summary)
                         · </summary> is searched only after <summary>
                         · <analysis> block is discarded
                         · a missing pair or an empty body falls back to the full text
    ```

  - In ⑥, replace the `**`<summary>` tag fallback**` bullet with:

    `- **`<summary>` extraction and fallback**: `_extract_summary()` (`context/compact.py`) finds `<summary>` and searches for `</summary>` only after it — a summarizer that mistakenly closes its `<analysis>` with `</summary>` would otherwise produce an empty summary and wipe the history. A missing pair or an empty body returns `None`, and the full response text is used instead. This is defensive code so the loop never halts on a format slip; lower format compliance degrades summary quality, so take care when modifying the prompt.`

- [ ] **Step 6: Full suite** — Run: `python -m pytest -q` → Expected: 256 passed.

- [ ] **Step 7: Commit**

```bash
git rev-parse --abbrev-ref HEAD
git add friday_agent/context/compact.py tests/test_compact.py docs/architecture/04-context-compaction.md
git commit -F - -- friday_agent/context/compact.py tests/test_compact.py docs/architecture/04-context-compaction.md <<'EOF'
fix(compact): pair </summary> only after <summary>, fall back on an empty body

A summarizer closing its <analysis> block with </summary> put a closing
tag ahead of the real opening one, and the extracted summary came out
empty — replacing the whole history with nothing.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JRQLSczEWcmgFp8vJsUs9q
EOF
git show --stat HEAD
```

---

### Task 2: `Terminal` carries the final state; remove the dead backfill

**Files:**
- Modify: `friday_agent/core/state.py:18-27`
- Modify: `friday_agent/core/loop.py` (module docstring, imports, delete `yield_missing_tool_result_blocks` at lines 76-112, rewrite `run_one_turn` at 167-276)
- Modify: `tests/test_loop_termination.py:103-130` (rename one test)
- Create: `tests/test_terminal_state.py`
- Modify: `docs/architecture/01-core-loop.md`, `06-invariants.md`, `07-data-models.md`, `CLAUDE.md`, `README.md`

**Interfaces:**
- Produces: `Terminal(reason: str, error: Exception | None = None, state: LoopState | None = None)`.
- Produces: `run_one_turn` that yields `Terminal(..., state=...)` on both exits and no backfill. Its body (`tool_use_blocks`, `tool_results`, `effects`) is the base Tasks 7 and 8 edit.

- [ ] **Step 1: Write the failing tests** — create `tests/test_terminal_state.py`:

```python
"""Terminal.state — every Terminal the loop emits carries the state to persist."""
import pytest

from friday_agent.api.provider import AssistantResponse, LLMError, StopReason, TextBlock, TokenUsage
from friday_agent.core.engine import FridayAgent
from friday_agent.core.state import LoopState, Terminal
from friday_agent.messages.types import create_user_message
from tests._drive import collect_turn
from tests.fakes import FakeLLMProvider


def _text(text: str) -> AssistantResponse:
    return AssistantResponse(content=[TextBlock(text=text)], stop_reason=StopReason.END_TURN, usage=TokenUsage())


@pytest.mark.asyncio
async def test_completed_terminal_carries_input_plus_assistant():
    fake = FakeLLMProvider(responses=[_text("hello")])
    engine = FridayAgent(provider=fake)
    todos = [{"content": "a", "status": "in_progress"}]
    state = LoopState(messages=[create_user_message("hi")], turn_count=3, todos=todos)

    messages, outcome = await collect_turn(engine, state)

    assert isinstance(outcome, Terminal) and outcome.reason == "completed"
    assert len(messages) == 1 and messages[0].type == "assistant"
    assert outcome.state.messages == [*state.messages, messages[0]]
    assert outcome.state.todos == todos
    assert outcome.state.turn_count == 4


@pytest.mark.asyncio
async def test_model_error_terminal_carries_input_state_for_retry():
    fake = FakeLLMProvider(responses=[_text("recovered")], errors={0: LLMError("boom")})
    engine = FridayAgent(provider=fake)
    state = LoopState(messages=[create_user_message("hi")], turn_count=2)

    _, outcome = await collect_turn(engine, state)
    assert outcome.reason == "model_error"
    assert outcome.state is state

    _, retried = await collect_turn(engine, outcome.state)
    assert retried.reason == "completed"
    assert fake.received_messages[1] == fake.received_messages[0]


@pytest.mark.asyncio
async def test_completed_state_survives_serde_and_continues():
    fake = FakeLLMProvider(responses=[_text("first"), _text("second")])
    engine = FridayAgent(provider=fake)
    _, first = await collect_turn(engine, LoopState(messages=[create_user_message("q1")]))

    restored = LoopState.from_dict(first.state.to_dict())
    restored.messages.append(create_user_message("q2"))
    _, second = await collect_turn(engine, restored)

    assert second.reason == "completed"
    assert [m["role"] for m in fake.received_messages[1]] == ["user", "assistant", "user"]
```

- [ ] **Step 2: Run and confirm they fail**

Run: `python -m pytest tests/test_terminal_state.py -v`
Expected: FAIL — `TypeError`/`AttributeError` (`Terminal` has no `state`).

- [ ] **Step 3: Implement `Terminal.state`** — in `friday_agent/core/state.py`, replace the `Terminal` class:

```python
@dataclass
class Terminal:
    """Returned when the agent loop terminates.

    reason values:
      'completed'    — normal end-turn exit
      'model_error'  — API or network error

    state is the state to persist, always set by the loop: for 'completed', the
    input state plus this turn's assistant message (turn_count + 1); for
    'model_error', the input state itself — step(terminal.state) repeats the
    failed call.
    """
    reason: str
    error: Exception | None = None
    state: LoopState | None = None
```

(`state.py` already has `from __future__ import annotations`, so the forward reference to `LoopState` is fine.)

- [ ] **Step 4: Rewrite the loop exits** — in `friday_agent/core/loop.py`:
  1. Replace the module docstring:

     ```python
     """run_one_turn() — a single iteration of the agent loop.

     Executes one turn: calls the provider, emits the assistant message, runs its
     tool_use blocks, and feeds the tool_results back. On a context overflow the
     provider's ContextOverflowError propagates to the caller (caller-owned
     compaction). The only other error path — an LLMError from the provider call —
     fires before any assistant message exists, so no tool_use is ever left unpaired.

     A turn ends by yielding exactly one sentinel: Terminal (loop done; carries the
     state to persist) or the next LoopState (loop may continue). The caller drives
     the turn loop by calling run_one_turn() in a while-true, advancing state on each
     LoopState until a Terminal appears — there is no batch driver and no internal
     compaction.
     """
     ```

  2. In the `friday_agent.messages.types` import, delete `create_tool_result_message,` (its only user is going away).
  3. Delete the whole `yield_missing_tool_result_blocks` function.
  4. Replace `run_one_turn` (signature unchanged) with:

```python
async def run_one_turn(
    *,
    provider: LLMProvider,
    tools: list[Tool],
    tool_schemas: list[dict],
    state: LoopState,
    system_prompt: str = "",
    config: LLMConfig | None = None,
    max_concurrency: int = 10,
    turn_reminders: list[str] | None = None,
) -> AsyncGenerator[Message | Terminal | LoopState, None]:
    """Execute a single turn of the agent loop.

    Yields all Messages produced in this turn, then yields exactly one sentinel:
      - Terminal: loop ends (completed / model_error); Terminal.state is the
        state to persist.
      - LoopState: loop continues (next_turn) — the updated state for the next turn.

    Args:
        provider: LLM backend; only complete() is called.
        tools: Available tool instances.
        tool_schemas: Pre-built JSON schemas for each tool.
        state: Input loop state restored from the previous turn or initial state.
        system_prompt: Base system prompt text.
        config: LLM call configuration. Defaults to provider.config_type().
        max_concurrency: Maximum concurrent tool executions passed to run_tools.
        turn_reminders: Pre-rendered turn-local reminder texts (e.g. a live
            memory index) appended after the todo reminder onto the trailing
            user message of the API view only — never persisted into LoopState.

    Yields:
        Message: messages produced this turn (assistant response, tool_result messages).
        Terminal | LoopState: exactly one sentinel as the final yield —
            Terminal when the loop ends, LoopState when it continues.

    Raises:
        ContextOverflowError: when the provider rejects the messages as too long.
            The caller should compact state via engine.compact() and retry.
    """
    config = config or provider.config_type()

    # API view only (turn-local, never persisted): inject the live todo reminder
    # and any caller-supplied turn reminders (e.g. memory index) so the model
    # sees current state without it entering the durable history. The next
    # state is assembled from the CLEAN state.messages below (no reminder leak).
    reminder_texts = [render_todo_reminder(state.todos)] if state.todos else []
    reminder_texts.extend(turn_reminders or [])
    api_input_messages = with_turn_reminders(list(state.messages), reminder_texts)

    # Assemble the full system prompt for this turn.
    full_system_prompt = assemble_system_prompt(system_prompt)

    # Call the LLM.
    api_messages = normalize_for_api(api_input_messages)
    try:
        response = await provider.complete(
            messages=api_messages,
            system_prompt=str(full_system_prompt),
            tools=tool_schemas,
            config=config,
        )
    except ContextOverflowError:
        # Caller-owned compaction: propagate so the caller can compact and retry.
        raise
    except LLMError as error:
        # The call failed before any assistant message existed: nothing to pair,
        # nothing to add. The input state is the state to persist (and retry).
        yield Terminal(reason="model_error", error=error, state=state)
        return

    # Convert the response to an internal Message and yield it.
    message = _to_assistant_message(response)
    yield message

    tool_use_blocks = _extract_tool_use_blocks(message)

    # Termination point: no tool_use blocks — the model is done.
    if not tool_use_blocks:
        yield Terminal(
            reason="completed",
            state=LoopState(
                messages=[*state.messages, message],
                turn_count=state.turn_count + 1,
                todos=state.todos,
            ),
        )
        return

    # Execute all tool_use blocks, collecting results and declarative state effects.
    effects: list[dict] = []
    tool_results: list[Message] = []
    async for result_msg in run_tools(
        tool_use_blocks, tools, max_concurrency=max_concurrency, effects_sink=effects
    ):
        tool_results.append(result_msg)
        yield result_msg

    # Continuation: assemble the next-turn LoopState from the CLEAN state.messages
    # (NOT api_input_messages) so the turn-local reminder is never persisted.
    yield LoopState(
        messages=[*state.messages, message, *tool_results],
        turn_count=state.turn_count + 1,
        todos=apply_state_effects(state.todos, effects),
    )
```

- [ ] **Step 5: Rename the backfill-named test** — in `tests/test_loop_termination.py`, rename `test_model_error_after_tool_use_backfills` to `test_model_error_after_tool_use_keeps_pairing`, and replace its docstring with:

```python
    """Pairing holds when model_error fires on the turn after a completed tool call.

    Turn 1: tool_use(t1) is executed and its tool_result is produced.
    Turn 2: the API raises — the error precedes any assistant message, so every
    tool_use yielded so far already has its tool_result.
    """
```

- [ ] **Step 6: Run the tests**

Run: `python -m pytest tests/test_terminal_state.py tests/test_loop_termination.py -v` → Expected: all PASS.
Run: `python -m pytest -q` → Expected: 259 passed.

- [ ] **Step 7: Update the docs**
  - `docs/architecture/01-core-loop.md`
    - ② table: replace the `core/loop.py` row with `| `friday_agent/core/loop.py` | Single-turn execution · stop_reason branching | `run_one_turn()` |`.
    - ③ Execution Order: replace `      Terminal               ─ loop terminates` with `      Terminal               ─ loop terminates; .state = the state to persist`.
    - ③ Branch table: replace the first two rows with
      `| No tool_use (non-tool stop such as end_turn) | `Terminal(reason="completed", state=…)` — `state` = input + assistant message, `turn_count+1` |`
      and
      `| `LLMError` (excluding overflow) | `Terminal(reason="model_error", error=..., state=…)` — `state` = the input state; retry with `step(terminal.state)` |`.
    - Replace the whole `### Backfill (`yield_missing_tool_result_blocks`)` subsection with:

      ```
      ### Final State on `Terminal`

      Every `Terminal` carries `state` — the state to persist. `completed`: the input state plus this turn's assistant message (`turn_count+1`, `todos` unchanged). `model_error`: the input state itself — the provider call failed before any assistant message existed, so there is nothing to add and no `tool_use` to pair; `step(terminal.state)` repeats the call. No extra `LoopState` is yielded before a `Terminal` (`LoopState` means "continue").
      ```

    - ④ step() example: replace `                              # Terminal  → loop terminates` with `                              # Terminal  → loop terminates (outcome.state = state to keep)`.
    - ④ State Types table: replace the `Terminal(reason, error=None)` row with `| `Terminal(reason, error=None, state=None)` | `core/state.py:19` | Loop termination sentinel; `state` is always set by the loop |`.
    - ⑥: replace the `**`tool_use↔tool_result` pair preservation.**` bullet with: `- **`tool_use↔tool_result` pair preservation.** `run_tools()` emits exactly one `tool_result` per executed `tool_use` (unknown tools and exceptions become error results), and the only error path (`LLMError` from the provider call) fires before an assistant message exists — so no unpaired `tool_use` is ever persisted. If this invariant breaks, the next API call fails immediately. See [06-invariants](06-invariants.md) for details.`
  - `docs/architecture/06-invariants.md` — replace the first table row with:
    `| Every `tool_use` has a matching `tool_result` | LLM API rejects the request | `core/loop.py` `run_one_turn()` — `run_tools()` emits exactly one `tool_result` per executed `tool_use` (unknown tools and exceptions become error results); the only error path (`LLMError` from the provider call) fires before an assistant message exists, so no unpaired `tool_use` is ever persisted → [01-core-loop](01-core-loop.md) |`
  - `docs/architecture/07-data-models.md` — in the `Terminal` table, add the row `| `state` | `LoopState \| None` | State to persist — `completed`: input + assistant message (`turn_count+1`); `model_error`: the input state. Always set by the loop |`.
  - `CLAUDE.md` — in the **Terminal** bullet, after `...so there is no `prompt_too_long` termination reason.`, insert: ` Every `Terminal` carries `state` — the state to persist (`completed`: input + assistant message; `model_error`: the input state, retried with `step(terminal.state)`).`
  - `README.md` — replace `If the last item received is a `Terminal`, stop the loop. `terminal.reason` is one of `completed` / `model_error`.` with `If the last item received is a `Terminal`, stop the loop. `terminal.reason` is one of `completed` / `model_error`, and `terminal.state` is the state to keep — append the next user message to it to continue the conversation, or pass it to `step()` again to retry a `model_error`.`

- [ ] **Step 8: Commit**

```bash
git rev-parse --abbrev-ref HEAD
git add friday_agent/core/state.py friday_agent/core/loop.py tests/test_terminal_state.py tests/test_loop_termination.py docs/architecture/01-core-loop.md docs/architecture/06-invariants.md docs/architecture/07-data-models.md CLAUDE.md README.md
git commit -F - -- friday_agent/core/state.py friday_agent/core/loop.py tests/test_terminal_state.py tests/test_loop_termination.py docs/architecture/01-core-loop.md docs/architecture/06-invariants.md docs/architecture/07-data-models.md CLAUDE.md README.md <<'EOF'
feat(core): Terminal carries the final state; drop the dead backfill

Terminal.state is always set by the loop: completed = input + assistant
message (turn_count + 1), model_error = the input state, so
step(terminal.state) retries. Every turn-ending sentinel now carries the
state to persist.

yield_missing_tool_result_blocks always returned an empty list — the
LLMError path fires before any assistant message exists — so it and the
docs that cited it as the pairing guarantee are gone.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JRQLSczEWcmgFp8vJsUs9q
EOF
git show --stat HEAD
```

---

### Task 3: OpenAI adapter — tool messages before trailing user text

**Files:**
- Modify: `friday_agent/api/openai_provider.py:152-221`
- Modify: `docs/architecture/03-llm-providers.md` (④ table)
- Test: `tests/test_openai_provider.py`

**Interfaces:** none new.

- [ ] **Step 1: Write the failing tests** — add after `test_to_openai_messages_assistant_tool_only_has_null_content` in `tests/test_openai_provider.py`:

```python
def test_to_openai_messages_tool_results_precede_trailing_text():
    """A reminder riding a tool_result turn must not split tool_calls from their tool messages."""
    messages = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "c1", "name": "f", "input": {}},
            {"type": "tool_use", "id": "c2", "name": "f", "input": {}},
        ]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "r1"}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "c2", "content": "r2"},
            {"type": "text", "text": "<system-reminder>\ntodo\n</system-reminder>"},
        ]},
    ]
    out = OpenAIProvider._to_openai_messages(messages, "")

    assert [m["role"] for m in out] == ["assistant", "tool", "tool", "user"]
    assert [m.get("tool_call_id") for m in out[1:3]] == ["c1", "c2"]
    assert out[3]["content"].startswith("<system-reminder>")


def test_to_openai_messages_single_user_turn_with_many_results_and_text():
    messages = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "c1", "name": "f", "input": {}},
            {"type": "tool_use", "id": "c2", "name": "f", "input": {}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "c1", "content": "r1"},
            {"type": "tool_result", "tool_use_id": "c2", "content": "r2"},
            {"type": "text", "text": "follow-up"},
        ]},
    ]
    out = OpenAIProvider._to_openai_messages(messages, "")

    assert [(m["role"], m.get("tool_call_id")) for m in out] == [
        ("assistant", None), ("tool", "c1"), ("tool", "c2"), ("user", None),
    ]
```

- [ ] **Step 2: Run and confirm they fail**

Run: `python -m pytest tests/test_openai_provider.py -k "precede or many_results" -v`
Expected: FAIL — the roles come out as `[..., 'user', 'tool']`.

- [ ] **Step 3: Implement** — in `_to_openai_messages`, replace the `else:` branch at the end of the loop body:

```python
            else:
                # user turn: tool results become tool messages, emitted FIRST —
                # OpenAI requires tool messages to directly follow the assistant's
                # tool_calls, and turn-local reminders ride the same user turn as
                # the results. Any text follows as a user message.
                out.extend(tool_msgs)
                if text_parts:
                    out.append({"role": "user", "content": "".join(text_parts)})
```

In the docstring's conversion rules, replace `- tool_result blocks (user)   → separate {"role": "tool", tool_call_id, content} message` with `- tool_result blocks (user)   → separate {"role": "tool", tool_call_id, content} messages, emitted before that turn's text`.

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_openai_provider.py -v` → Expected: all PASS.
Run: `python -m pytest -q` → Expected: 261 passed.

- [ ] **Step 5: Update `docs/architecture/03-llm-providers.md`** — in the ④ table, replace the OpenAI cell of the **Message format** row with: ``tool_use`→`tool_calls`, `tool_result`→`{"role":"tool"}` (emitted **before** the same turn's text — reminders ride the tool_result turn, and tool messages must directly follow `tool_calls`), system→leading message, thinking dropped (`_to_openai_messages`)`.

- [ ] **Step 6: Commit**

```bash
git rev-parse --abbrev-ref HEAD
git add friday_agent/api/openai_provider.py tests/test_openai_provider.py docs/architecture/03-llm-providers.md
git commit -F - -- friday_agent/api/openai_provider.py tests/test_openai_provider.py docs/architecture/03-llm-providers.md <<'EOF'
fix(openai): emit tool messages before a turn's trailing text

Turn-local reminders ride the trailing tool_result turn. The adapter sent
that turn's text first, so OpenAI received assistant(tool_calls) -> user ->
tool and rejected the request whenever todos or memory were active.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JRQLSczEWcmgFp8vJsUs9q
EOF
git show --stat HEAD
```

---

### Task 4: Per-turn sections hook (`turn_sections`)

**Files:**
- Modify: `friday_agent/core/engine.py` (imports, class docstring, `__init__`, `step`)
- Create: `tests/test_engine_turn_sections.py`
- Modify: `docs/architecture/01-core-loop.md`, `00-overview.md`, `CLAUDE.md`, `README.md`

**Interfaces:**
- Produces: `TurnSection = Callable[[LoopState], Awaitable[str]]` (in `friday_agent/core/engine.py`).
- Produces: `FridayAgent(..., turn_sections: list[TurnSection] | None = None)`, stored as `self._turn_sections: list[TurnSection]`.

- [ ] **Step 1: Write the failing tests** — create `tests/test_engine_turn_sections.py`:

```python
"""FridayAgent(turn_sections=...) — per-turn sections ride messages[-1] only."""
import pytest

from friday_agent.api.provider import AssistantResponse, StopReason, TextBlock, TokenUsage, ToolUseBlock
from friday_agent.core.engine import FridayAgent
from friday_agent.core.state import LoopState
from friday_agent.memory.store import MemoryEntry, MemoryType
from friday_agent.messages.types import SYSTEM_REMINDER_PREFIX, create_user_message
from friday_agent.tools.builtin.example_tool import ExampleTool
from tests._drive import collect_turn
from tests.fakes import FakeLLMProvider, InMemoryStore


def _text(text: str = "done") -> AssistantResponse:
    return AssistantResponse(content=[TextBlock(text=text)], stop_reason=StopReason.END_TURN, usage=TokenUsage())


def _tool_call(tool_id: str) -> AssistantResponse:
    return AssistantResponse(
        content=[ToolUseBlock(id=tool_id, name="ExampleTool", input={"payload": "x"})],
        stop_reason=StopReason.TOOL_USE,
        usage=TokenUsage(),
    )


def _texts(api_message: dict) -> list[str]:
    return [b.get("text", "") for b in api_message["content"] if b.get("type") == "text"]


async def _turns_section(state: LoopState) -> str:
    return f"SECTION turns={state.turn_count}"


@pytest.mark.asyncio
async def test_section_rides_last_user_message_wrapped_as_reminder():
    fake = FakeLLMProvider(responses=[_text()])
    engine = FridayAgent(provider=fake, turn_sections=[_turns_section])

    await collect_turn(engine, LoopState(messages=[create_user_message("hi")], turn_count=7))

    last = fake.received_messages[0][-1]
    assert last["role"] == "user"
    texts = _texts(last)
    assert texts[0] == "hi"
    assert texts[1].startswith(SYSTEM_REMINDER_PREFIX)
    assert "SECTION turns=7" in texts[1]  # the section received the input state


@pytest.mark.asyncio
async def test_section_never_persisted():
    fake = FakeLLMProvider(responses=[_tool_call("t1")])
    engine = FridayAgent(provider=fake, tools=[ExampleTool()], turn_sections=[_turns_section])

    _, outcome = await collect_turn(engine, LoopState(messages=[create_user_message("hi")]))

    assert isinstance(outcome, LoopState)
    assert "SECTION" not in str(outcome.to_dict())


@pytest.mark.asyncio
async def test_without_sections_request_is_unchanged():
    default = FakeLLMProvider(responses=[_text()])
    empty = FakeLLMProvider(responses=[_text()])
    state = LoopState(messages=[create_user_message("hi")])

    await collect_turn(FridayAgent(provider=default), state)
    await collect_turn(FridayAgent(provider=empty, turn_sections=[]), state)

    assert default.received_messages[0] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    assert empty.received_messages[0] == default.received_messages[0]


@pytest.mark.asyncio
async def test_empty_section_output_is_dropped():
    async def silent(state: LoopState) -> str:
        return ""

    fake = FakeLLMProvider(responses=[_text()])
    await collect_turn(FridayAgent(provider=fake, turn_sections=[silent]), LoopState(messages=[create_user_message("hi")]))

    assert _texts(fake.received_messages[0][-1]) == ["hi"]


@pytest.mark.asyncio
async def test_order_todo_then_memory_then_sections():
    store = InMemoryStore()
    await store.save(MemoryEntry(name="pref", description="likes tea", type=MemoryType.user, body="tea"))

    async def first(state: LoopState) -> str:
        return "FIRST"

    async def second(state: LoopState) -> str:
        return "SECOND"

    fake = FakeLLMProvider(responses=[_text()])
    engine = FridayAgent(provider=fake, memory=store, turn_sections=[first, second])
    state = LoopState(messages=[create_user_message("hi")], todos=[{"content": "a", "status": "pending"}])

    await collect_turn(engine, state)

    joined = "\n".join(_texts(fake.received_messages[0][-1]))
    assert (
        joined.index("Current todo list")
        < joined.index("likes tea")
        < joined.index("FIRST")
        < joined.index("SECOND")
    )


@pytest.mark.asyncio
async def test_cache_prefix_stable_across_turns():
    """Everything before messages[-1] is byte-identical turn to turn."""
    ticks = {"n": 0}

    async def ticker(state: LoopState) -> str:
        ticks["n"] += 1
        return f"tick {ticks['n']}"

    fake = FakeLLMProvider(responses=[_tool_call("t1"), _tool_call("t2"), _text()])
    engine = FridayAgent(provider=fake, tools=[ExampleTool()], turn_sections=[ticker])

    state = LoopState(messages=[create_user_message("hi")])
    _, state = await collect_turn(engine, state)
    _, state = await collect_turn(engine, state)
    await collect_turn(engine, state)

    _, second, third = fake.received_messages
    assert third[: len(second) - 1] == second[:-1]  # messages[-2] and earlier unchanged
    assert "tick 2" not in str(third)  # last turn's section did not persist
    assert "tick 3" in str(third[-1])


@pytest.mark.asyncio
async def test_section_exception_propagates():
    async def broken(state: LoopState) -> str:
        raise RuntimeError("section failed")

    fake = FakeLLMProvider(responses=[_text()])
    with pytest.raises(RuntimeError, match="section failed"):
        await collect_turn(
            FridayAgent(provider=fake, turn_sections=[broken]),
            LoopState(messages=[create_user_message("hi")]),
        )
    assert fake.call_count == 0
```

- [ ] **Step 2: Run and confirm they fail**

Run: `python -m pytest tests/test_engine_turn_sections.py -v`
Expected: FAIL — `TypeError: FridayAgent.__init__() got an unexpected keyword argument 'turn_sections'`. (`test_without_sections_request_is_unchanged` fails the same way, on its second engine.)

- [ ] **Step 3: Implement** — in `friday_agent/core/engine.py`:
  1. Imports: change `from typing import AsyncGenerator` to `from typing import AsyncGenerator, Awaitable, Callable`, and `from friday_agent.messages.types import Message` to `from friday_agent.messages.types import Message, wrap_system_reminder`.
  2. Below the imports, add:

     ```python
     # A per-turn section: rendered from the turn's input state on every step();
     # its output rides the trailing user message as a <system-reminder> (never
     # persisted, never part of the cached prefix).
     TurnSection = Callable[[LoopState], Awaitable[str]]
     ```

  3. Class docstring `Args:` — after the `compact_instructions` entry, add:

     ```
             turn_sections: Async callables rendered on every step() from the turn's
                     input state. Each non-empty output is wrapped in a
                     <system-reminder> and joined onto the trailing user message of
                     the API view only (after the todo reminder and the memory
                     index) — never persisted into LoopState, never part of the
                     cached prefix. Use for content that changes during a session
                     (current screen, progress); static content belongs in
                     system_prompt. Empty strings are dropped; exceptions propagate.
     ```

  4. `__init__` signature: after `compact_instructions: str = "",` add `turn_sections: list[TurnSection] | None = None,`. At the end of `__init__`, add `self._turn_sections = list(turn_sections or [])`.
  5. In `step()`, replace the comment block and the `turn_reminders` lines (from `# System prefix: static pieces only` through `turn_reminders = [t for t in turn_reminders if t]`) with:

     ```python
             # System prefix: static pieces only, ordered generic -> specific
             # (memory instructions -> domain prompt) — must be byte-stable within a
             # session so the cache prefix survives. Per-turn content (the live memory
             # index, then turn_sections outputs) is rebuilt every turn and rides
             # messages[-1] as turn-local reminders instead — in the system prompt it
             # would invalidate the whole conversation cache.
             memory_section = MEMORY_INSTRUCTIONS if self._memory is not None else ""
             parts = [p for p in (memory_section, self._system_prompt) if p]
             effective_prompt = "\n\n".join(parts)
             turn_reminders = (
                 [await build_memory_reminder(self._memory)] if self._memory is not None else []
             )
             for section in self._turn_sections:
                 text = await section(state)
                 if text:
                     turn_reminders.append(wrap_system_reminder(text))
             turn_reminders = [t for t in turn_reminders if t]
     ```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_engine_turn_sections.py -v` → Expected: all PASS.
Run: `python -m pytest -q` → Expected: 268 passed.

- [ ] **Step 5: Update the docs**
  - `docs/architecture/01-core-loop.md`
    - ③ Execution Order: replace `+ turn_reminders param (engine passes memory index)` with `+ turn_reminders param (engine passes memory index, then turn_sections outputs)`.
    - ④ constructor block: after the `compact_instructions=""` line, add `    turn_sections=None,    # per-turn sections: async (state) -> str, rendered every step(); turn-local <system-reminder> on messages[-1], never persisted`.
    - ④: replace the paragraph starting `The context injection surface is intentionally simple:` with:

      `The context injection surface is intentionally simple: static content is passed by the caller as a single `system_prompt` string (multiple sections are combined on the caller side with `"\n\n".join(...)`). Per-turn content has exactly one engine-level hook, `turn_sections`: each section is awaited with the turn's input `LoopState`, empty output is dropped, and the SDK wraps the rest in `<system-reminder>` (the prefix the Anthropic adapter's breakpoint skip detects) and joins it onto the trailing user message after the todo reminder and the memory index — the `turn_reminders` path of `run_one_turn`. A section that raises propagates.`

    - ⑥: at the end of the **Per-turn reminders are non-persistent** bullet, append: ` `turn_sections` outputs follow the same path (after the memory index), so they never reach `LoopState`, the cached prefix, or `compact()`.`
  - `docs/architecture/00-overview.md` — after the paragraph that starts `In addition, `FridayAgent` always registers`, add: `Per-turn state the model should see (current screen, progress) goes through `turn_sections` — rendered every turn into a turn-local `<system-reminder>` on the last user message, never persisted and never part of the cached prefix (see [01-core-loop](01-core-loop.md)).`
  - `CLAUDE.md` — replace `and there are no engine-level dynamic injection hooks.` with `and per-turn content has exactly one engine-level hook, `turn_sections` (async `(state) -> str` callables; the SDK wraps each output in `<system-reminder>` and carries it on `messages[-1]` only — never persisted).`
  - `README.md` — after the `### Injecting Domain Requirements into Compaction (opt-in)` section, before the `---`, add:

    ````markdown
    ### Per-Turn Context (opt-in)

    For state that changes every turn (the current screen, progress so far), pass async sections. Each one receives the turn's input `LoopState` and returns text (`""` = nothing this turn):

    ```python
    async def current_page(state):
        return f"Current page: {browser.url}"

    engine = FridayAgent(provider=provider, turn_sections=[current_page])
    ```

    Each non-empty output is wrapped in `<system-reminder>` and attached to the last user message of that turn's request only — it is never stored in `LoopState` and never breaks the prompt cache. Static content belongs in `system_prompt`.
    ````

- [ ] **Step 6: Commit**

```bash
git rev-parse --abbrev-ref HEAD
git add friday_agent/core/engine.py tests/test_engine_turn_sections.py docs/architecture/01-core-loop.md docs/architecture/00-overview.md CLAUDE.md README.md
git commit -F - -- friday_agent/core/engine.py tests/test_engine_turn_sections.py docs/architecture/01-core-loop.md docs/architecture/00-overview.md CLAUDE.md README.md <<'EOF'
feat(engine): turn_sections — per-turn context on messages[-1]

FridayAgent(turn_sections=[async (state) -> str, ...]) renders each section
every step(); the SDK wraps non-empty output in <system-reminder> and joins
it onto the trailing user message after the todo reminder and the memory
index. Never persisted, never part of the cached prefix.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JRQLSczEWcmgFp8vJsUs9q
EOF
git show --stat HEAD
```

---

### Task 5: Image-bearing tool results

**Files:**
- Modify: `friday_agent/tools/base.py:16-21` (`ToolResult`)
- Modify: `friday_agent/messages/types.py:15,98-112`
- Modify: `friday_agent/tools/orchestrator.py` (imports, new `to_tool_result_message`, `_run_single_tool`)
- Modify: `friday_agent/messages/normalize.py:48-56` (comment)
- Modify: `friday_agent/api/openai_provider.py` (new `_flatten_tool_result_content`, tool_result branch, docstring)
- Create: `tests/test_tool_result_image.py`
- Modify: `docs/architecture/05-messages.md`, `03-llm-providers.md`, `07-data-models.md`, `02-tool-orchestration.md`, `README.md`

**Interfaces:**
- Produces: `ToolResult(data, is_error=False, state_effect=None, image: dict | None = None)`.
- Produces: `create_tool_result_message(tool_use_id: str, result_text: str, is_error: bool = False, image: dict | None = None) -> Message`.
- Produces: `to_tool_result_message(tool_use_id: str, result: ToolResult) -> Message` in `friday_agent/tools/orchestrator.py`. Task 8's `resume()` uses it.

- [ ] **Step 1: Write the failing tests** — create `tests/test_tool_result_image.py`:

```python
"""ToolResult.image — tool_result content becomes a [text, image] block array."""
import json

import pytest
from pydantic import BaseModel

from friday_agent.api.anthropic_provider import AnthropicProvider
from friday_agent.api.configs import AnthropicConfig
from friday_agent.api.openai_provider import OpenAIProvider
from friday_agent.api.provider import AssistantResponse, StopReason, TextBlock, TokenUsage, ToolUseBlock
from friday_agent.core.engine import FridayAgent
from friday_agent.core.state import LoopState
from friday_agent.messages.normalize import normalize_for_api
from friday_agent.messages.types import create_tool_result_message, create_user_message
from friday_agent.tools.base import Tool, ToolResult
from friday_agent.tools.builtin.example_tool import ExampleTool
from friday_agent.tools.orchestrator import to_tool_result_message
from tests._drive import collect_turn
from tests.fakes import FakeLLMProvider

IMAGE = {"media_type": "image/png", "data": "iVBORw0KGgo="}
MARKER = "[image omitted: not supported by the OpenAI adapter]"


class _NoInput(BaseModel):
    pass


class _Screenshot(Tool):
    """Returns a screenshot."""

    name = "screenshot"

    def input_schema(self) -> type[BaseModel]:
        return _NoInput

    async def call(self, args: dict) -> ToolResult:
        return ToolResult(data="captured", image=IMAGE)


def test_message_without_image_keeps_string_content():
    assert create_tool_result_message("t1", "ok").content[0].content == "ok"


def test_message_with_image_is_text_then_image_blocks():
    msg = create_tool_result_message("t1", "ok", image=IMAGE)
    assert msg.content[0].content == [
        {"type": "text", "text": "ok"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}},
    ]


def test_error_with_image_wraps_text_only():
    blocks = create_tool_result_message("t1", "bad", is_error=True, image=IMAGE).content[0].content
    assert blocks[0] == {"type": "text", "text": "<tool_use_error>bad</tool_use_error>"}
    assert blocks[1]["type"] == "image"


def test_to_tool_result_message_carries_data_error_and_image():
    msg = to_tool_result_message("t1", ToolResult(data=42, is_error=True, image=IMAGE))
    block = msg.content[0]
    assert block.tool_use_id == "t1" and block.is_error
    assert block.content[0]["text"] == "<tool_use_error>42</tool_use_error>"
    assert block.content[1]["source"]["data"] == IMAGE["data"]


def test_serde_round_trip_keeps_block_array():
    state = LoopState(messages=[create_tool_result_message("t1", "ok", image=IMAGE)])
    restored = LoopState.from_dict(json.loads(json.dumps(state.to_dict())))
    assert restored.messages[0].content[0].content == state.messages[0].content[0].content


def test_anthropic_request_carries_image_block():
    api = normalize_for_api([create_tool_result_message("t1", "ok", image=IMAGE)])
    params = AnthropicProvider(api_key="k", model="m")._build_params(api, "", [], AnthropicConfig())
    block = params["messages"][-1]["content"][0]
    assert block["type"] == "tool_result"
    assert block["content"][1] == {"type": "image", "source": {"type": "base64", **IMAGE}}


def test_openai_request_flattens_image_to_marker():
    api = normalize_for_api([create_tool_result_message("t1", "ok", image=IMAGE)])
    out = OpenAIProvider._to_openai_messages(api, "")
    assert out == [{"role": "tool", "tool_call_id": "t1", "content": f"ok\n{MARKER}"}]
    assert IMAGE["data"] not in json.dumps(out)


def test_openai_plain_string_result_unchanged():
    api = normalize_for_api([create_tool_result_message("t1", "plain")])
    assert OpenAIProvider._to_openai_messages(api, "") == [{"role": "tool", "tool_call_id": "t1", "content": "plain"}]


@pytest.mark.asyncio
async def test_tool_image_reaches_next_request():
    call = AssistantResponse(
        content=[ToolUseBlock(id="t1", name="screenshot", input={})],
        stop_reason=StopReason.TOOL_USE,
        usage=TokenUsage(),
    )
    done = AssistantResponse(content=[TextBlock(text="I see it")], stop_reason=StopReason.END_TURN, usage=TokenUsage())
    fake = FakeLLMProvider(responses=[call, done])
    engine = FridayAgent(provider=fake, tools=[_Screenshot()])

    _, state = await collect_turn(engine, LoopState(messages=[create_user_message("look")]))
    await collect_turn(engine, state)

    result_block = fake.received_messages[1][-1]["content"][0]
    assert result_block["content"][1]["type"] == "image"


@pytest.mark.asyncio
async def test_result_without_image_stays_a_string_in_the_request():
    call = AssistantResponse(
        content=[ToolUseBlock(id="t1", name="ExampleTool", input={"payload": "x"})],
        stop_reason=StopReason.TOOL_USE,
        usage=TokenUsage(),
    )
    done = AssistantResponse(content=[TextBlock(text="ok")], stop_reason=StopReason.END_TURN, usage=TokenUsage())
    fake = FakeLLMProvider(responses=[call, done])
    engine = FridayAgent(provider=fake, tools=[ExampleTool()])

    _, state = await collect_turn(engine, LoopState(messages=[create_user_message("go")]))
    await collect_turn(engine, state)

    assert fake.received_messages[1][-1]["content"][0]["content"] == "processed: x"
```

- [ ] **Step 2: Run and confirm they fail**

Run: `python -m pytest tests/test_tool_result_image.py -v`
Expected: collection ERROR — `ImportError: cannot import name 'to_tool_result_message'`.

- [ ] **Step 3: Implement**
  1. `friday_agent/tools/base.py` — add a field to `ToolResult`:

     ```python
         image: dict | None = None          # {"media_type": "image/png", "data": "<base64>"} — sent next to data
     ```

  2. `friday_agent/messages/types.py` — change the `content` field comment line to:

     ```python
         content: str | list[dict] | None = None   # type=="tool_result" — text, or [text, image] blocks
     ```

     and replace `create_tool_result_message` with:

     ```python
     def create_tool_result_message(
         tool_use_id: str,
         result_text: str,
         is_error: bool = False,
         image: dict | None = None,
     ) -> Message:
         """Build a user message carrying one tool_result block.

         Without an image the content is the result string (wrapped in
         <tool_use_error> when is_error). With an image ({"media_type", "data"}) it
         becomes a [text, image] block array, which tool_result accepts natively.
         """
         body = f"<tool_use_error>{result_text}</tool_use_error>" if is_error else result_text
         content: str | list[dict] = body if image is None else [
             {"type": "text", "text": body},
             {"type": "image", "source": {"type": "base64", **image}},
         ]
         return Message(
             type="user",
             role="user",
             content=[ContentBlock(
                 type="tool_result",
                 tool_use_id=tool_use_id,
                 content=content,
                 is_error=is_error,
             )],
         )
     ```

  3. `friday_agent/tools/orchestrator.py` — change `from friday_agent.tools.base import Tool` to `from friday_agent.tools.base import Tool, ToolResult`. Add this function above the "Single-tool execution helper" banner:

     ```python
     def to_tool_result_message(tool_use_id: str, result: ToolResult) -> Message:
         """Convert a ToolResult into its tool_result message (data, error flag, image).

         The single conversion shared by run_tools and core.loop.resume(), so results
         produced inside step() and results attached later behave identically.
         """
         return create_tool_result_message(
             tool_use_id=tool_use_id,
             result_text=str(result.data),
             is_error=result.is_error,
             image=result.image,
         )
     ```

     In `_run_single_tool`, replace the success `return` inside `try:` with:

     ```python
             result = await tool.call(block.input or {})
             return to_tool_result_message(block.id or "", result), result.state_effect
     ```

  4. `friday_agent/messages/normalize.py` — in the `tool_result` branch of `_convert_content_blocks`, add this comment above `entry: dict = {`:

     ```python
                 # content is a string, or a [text, image] block array for image
                 # results — both pass through as-is (adapters own the wire form).
     ```

  5. `friday_agent/api/openai_provider.py` — below `_CONTEXT_OVERFLOW_SIGNALS`, add:

     ```python
     # Stands in for an image block in a flattened tool_result: Chat Completions
     # tool messages are text-only, and serializing the base64 would add tokens
     # without the model ever seeing the image.
     _IMAGE_OMITTED = "[image omitted: not supported by the OpenAI adapter]"


     def _flatten_tool_result_content(content) -> str:
         """Flatten tool_result content to the text-only form OpenAI tool messages take.

         A string passes through; a block array joins its text blocks and replaces
         each image with _IMAGE_OMITTED; anything else is JSON-encoded.
         """
         if isinstance(content, str):
             return content
         if not isinstance(content, list):
             return json.dumps(content)
         parts: list[str] = []
         for block in content:
             if isinstance(block, dict) and block.get("type") == "text":
                 parts.append(block.get("text") or "")
             elif isinstance(block, dict) and block.get("type") == "image":
                 parts.append(_IMAGE_OMITTED)
             else:
                 parts.append(json.dumps(block))
         return "\n".join(p for p in parts if p)
     ```

     In `_to_openai_messages`'s `tool_result` branch, replace the two lines `content = b.get("content")` … `"content": content if isinstance(content, str) else json.dumps(content),` with:

     ```python
                 elif btype == "tool_result":
                     tool_msgs.append({
                         "role": "tool",
                         "tool_call_id": b.get("tool_use_id") or "",
                         "content": _flatten_tool_result_content(b.get("content")),
                     })
     ```

     In the class docstring's conversion rules, add the line `  - tool_result block arrays → text joined, images replaced by a marker (_flatten_tool_result_content)`.

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_tool_result_image.py -v` → Expected: all PASS.
Run: `python -m pytest -q` → Expected: 278 passed.

- [ ] **Step 5: Update the docs**
  - `docs/architecture/05-messages.md`
    - ③ `ContentBlock` table: replace the `content` row with `| `content` | `str \| list[dict] \| None` | `tool_result` | Tool result text — or a `[text, image]` block array when the tool returned an image |`.
    - `create_tool_result_message()` signature block: add the parameter `    image: dict | None = None,`. Under it, add the bullet: `- If `image` is given (`{"media_type", "data"}`, base64), `content` becomes `[{"type": "text", "text": ...}, {"type": "image", "source": {"type": "base64", ...}}]`; otherwise the string, exactly as before.`
    - Block conversion table: replace the `tool_result` output cell with `{"type":"tool_result", "tool_use_id":..., "content":...}` (content passed as-is — string or block array; adds `"is_error":True` if is_error=True)`.
  - `docs/architecture/03-llm-providers.md` — add a row to the ④ table, after **Message format**: `| **tool_result images** | `[text, image]` block array sent as-is | Flattened by `_flatten_tool_result_content`: text blocks joined, each image replaced by `[image omitted: not supported by the OpenAI adapter]` (Chat Completions tool messages are text-only) |`.
  - `docs/architecture/07-data-models.md`
    - `ContentBlock` table: replace the `tool_use_id`·`content`·`is_error` row's meaning with `Matching tool_use ID · result text (or a `[text, image]` block array) · error flag`.
    - `ToolResult` table: add `| `image` | `dict \| None` | `{"media_type", "data"}` (base64) — sent as an image block next to the text; default `None` |`.
  - `docs/architecture/02-tool-orchestration.md`
    - ② table: `tools/orchestrator.py` key symbols become `partition_tool_calls()`, `run_tools()`, `to_tool_result_message()`, `Batch`.
    - ③ data flow: replace `each block → _run_single_tool() → tool_result Message` with `each block → _run_single_tool() → to_tool_result_message() → tool_result Message`.
    - ④ `ToolResult` block: replace it with

      ```python
      ToolResult(
          data,                   # execution result (string or structured data)
          is_error=False,
          state_effect=None,      # declarative state mutation (e.g. {"todos": [...]}); applied solely by the loop
          image=None,             # {"media_type": "image/png", "data": "<base64>"} — sent as an image block next to the text
      )
      ```

      and add below it: `to_tool_result_message(tool_use_id, result)` is the single `ToolResult` → `tool_result` message conversion (data → text, `is_error` → `<tool_use_error>` wrapping, `image` → block array). It is shared by `run_tools` and `resume()`.
  - `README.md` — in `### ① Tools (Tool)`, after `Pass the tool you built to `FridayAgent(tools=[WeatherTool()])` and the model can call it.`, add: `A tool can return an image next to its text — `ToolResult(data="Captured.", image={"media_type": "image/png", "data": b64})`. Anthropic models see the image; the OpenAI adapter sends the text plus an `[image omitted ...]` marker.`

- [ ] **Step 6: Commit**

```bash
git rev-parse --abbrev-ref HEAD
git add friday_agent/tools/base.py friday_agent/messages/types.py friday_agent/tools/orchestrator.py friday_agent/messages/normalize.py friday_agent/api/openai_provider.py tests/test_tool_result_image.py docs/architecture/05-messages.md docs/architecture/03-llm-providers.md docs/architecture/07-data-models.md docs/architecture/02-tool-orchestration.md README.md
git commit -F - -- friday_agent/tools/base.py friday_agent/messages/types.py friday_agent/tools/orchestrator.py friday_agent/messages/normalize.py friday_agent/api/openai_provider.py tests/test_tool_result_image.py docs/architecture/05-messages.md docs/architecture/03-llm-providers.md docs/architecture/07-data-models.md docs/architecture/02-tool-orchestration.md README.md <<'EOF'
feat(tools): image-bearing tool results (ToolResult.image)

With an image, tool_result content becomes a [text, image] block array;
without one it is the same string as before. One conversion,
to_tool_result_message(), serves every ToolResult. Anthropic sends the
array as-is; OpenAI flattens it to text plus a marker so no base64 is
billed for nothing. LoopState serde round-trips the array.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JRQLSczEWcmgFp8vJsUs9q
EOF
git show --stat HEAD
```

---

### Task 6: Invariants doc — pending `tool_use` only in a `Suspended` state

**Files:**
- Modify: `docs/architecture/06-invariants.md`

**Interfaces:** documents the contracts Tasks 7–8 implement (`Suspended`, `pending_tool_uses()`, `PendingToolUseError`, `resume()`).

- [ ] **Step 1: Edit the intro** — in "What the Invariant Is", after `This constraint must not break on any execution path, including error recovery and parallel execution.`, insert: ` The single, explicit exception is a `Suspended` state: its last assistant message may hold deferred `tool_use` blocks whose results arrive later through `resume()` — and no request is ever sent while one is pending.`

- [ ] **Step 2: Replace row 1** with:

`| Every `tool_use` has a matching `tool_result` — **except** the trailing assistant message of a `Suspended` state, whose deferred calls are pending | LLM API rejects the request | `core/loop.py` `run_one_turn()` — `run_tools()` emits exactly one `tool_result` per executed `tool_use` (errors included) and deferred calls end the turn as `Suspended(state, pending)`; `pending_tool_uses()` recomputes pending calls from history, and `step()`/`compact()` raise `PendingToolUseError` before any request while one remains (also when a user message was appended after it); `resume()` accepts only pending ids. The only error path (`LLMError` from the provider call) fires before an assistant message exists → [01-core-loop](01-core-loop.md) |`

- [ ] **Step 3: Replace row 2** with:

`| Results keep the original `tool_use` block order — under parallel execution and when deferred results arrive later | Breaks pair matching and reproducibility | `tools/orchestrator.py` `run_tools()` — `asyncio.gather` returns results in argument order, so block order is preserved regardless of completion order; `core/loop.py` `resume()` inserts late results by `tool_use` order, ahead of any other trailing message → [02-tool-orchestration](02-tool-orchestration.md)·[01-core-loop](01-core-loop.md) |`

- [ ] **Step 4: Commit**

```bash
git rev-parse --abbrev-ref HEAD
git add docs/architecture/06-invariants.md
git commit -F - -- docs/architecture/06-invariants.md <<'EOF'
docs(invariants): a pending tool_use may exist only in a Suspended state

Contract first: deferred tool calls leave the trailing assistant message
unpaired until resume() attaches their results; step() and compact()
refuse to send such a state.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JRQLSczEWcmgFp8vJsUs9q
EOF
git show --stat HEAD
```

---

### Task 7: Pending-call guard — `pending_tool_uses()` + `PendingToolUseError`

**Files:**
- Modify: `friday_agent/core/state.py` (add `PendingToolUseError`)
- Modify: `friday_agent/core/loop.py` (add `_last_assistant_index`, `pending_tool_uses`; guard at the top of `run_one_turn`)
- Modify: `friday_agent/core/engine.py` (guard at the top of `step` and `compact`)
- Create: `tests/test_pending_guard.py`
- Modify: `docs/architecture/01-core-loop.md`, `04-context-compaction.md`, `07-data-models.md`

**Interfaces:**
- Produces: `class PendingToolUseError(ValueError)` with `.tool_use_ids: list[str]`, constructed as `PendingToolUseError(tool_use_ids)` (in `core/state.py`).
- Produces: `pending_tool_uses(state: LoopState) -> list[ContentBlock]` and `_last_assistant_index(messages: list[Message]) -> int | None` (in `core/loop.py`; Task 8 uses both).

- [ ] **Step 1: Write the failing tests** — create `tests/test_pending_guard.py`:

```python
"""pending_tool_uses() + PendingToolUseError — no request while a tool_use is unanswered."""
import pytest

from friday_agent.api.provider import LLMError
from friday_agent.core.engine import FridayAgent
from friday_agent.core.loop import pending_tool_uses, run_one_turn
from friday_agent.core.state import LoopState, PendingToolUseError
from friday_agent.messages.types import ContentBlock, Message, create_tool_result_message, create_user_message
from tests._drive import collect_turn
from tests.fakes import FakeLLMProvider


def _assistant_calls(*ids: str) -> Message:
    return Message(
        type="assistant",
        role="assistant",
        content=[ContentBlock(type="tool_use", id=i, name="f", input={}) for i in ids],
    )


def test_no_assistant_message_means_nothing_pending():
    assert pending_tool_uses(LoopState(messages=[])) == []
    assert pending_tool_uses(LoopState(messages=[create_user_message("hi")])) == []


def test_unanswered_calls_of_last_assistant_are_pending():
    state = LoopState(messages=[
        create_user_message("hi"),
        _assistant_calls("a", "b", "c"),
        create_tool_result_message("b", "ok"),
    ])
    assert [b.id for b in pending_tool_uses(state)] == ["a", "c"]


def test_fully_answered_turn_has_nothing_pending():
    state = LoopState(messages=[
        create_user_message("hi"),
        _assistant_calls("a"),
        create_tool_result_message("a", "ok"),
    ])
    assert pending_tool_uses(state) == []


@pytest.mark.asyncio
async def test_step_rejects_pending_state_before_anything_runs():
    fake = FakeLLMProvider(responses=[])
    rendered: list[LoopState] = []

    async def section(state: LoopState) -> str:
        rendered.append(state)
        return "S"

    engine = FridayAgent(provider=fake, turn_sections=[section])
    state = LoopState(messages=[create_user_message("hi"), _assistant_calls("a", "b")])

    with pytest.raises(PendingToolUseError) as exc:
        await collect_turn(engine, state)

    assert exc.value.tool_use_ids == ["a", "b"]
    assert fake.call_count == 0
    assert rendered == []


@pytest.mark.asyncio
async def test_step_rejects_pending_even_after_a_new_user_message():
    fake = FakeLLMProvider(responses=[])
    state = LoopState(messages=[
        create_user_message("hi"),
        _assistant_calls("a"),
        create_user_message("never mind"),
    ])

    with pytest.raises(PendingToolUseError):
        await collect_turn(FridayAgent(provider=fake), state)
    assert fake.call_count == 0


@pytest.mark.asyncio
async def test_run_one_turn_rejects_pending_state():
    fake = FakeLLMProvider(responses=[])
    state = LoopState(messages=[create_user_message("hi"), _assistant_calls("a")])

    with pytest.raises(PendingToolUseError):
        async for _ in run_one_turn(provider=fake, tools=[], tool_schemas=[], state=state):
            pass
    assert fake.call_count == 0


@pytest.mark.asyncio
async def test_compact_rejects_pending_state():
    fake = FakeLLMProvider(responses=[])
    state = LoopState(messages=[create_user_message("hi"), _assistant_calls("a")])

    with pytest.raises(PendingToolUseError):
        await FridayAgent(provider=fake).compact(state)
    assert fake.call_count == 0


def test_pending_error_is_a_value_error_not_an_llm_error():
    err = PendingToolUseError(["a"])
    assert isinstance(err, ValueError) and not isinstance(err, LLMError)
    assert err.tool_use_ids == ["a"]
    assert "resume()" in str(err)
```

- [ ] **Step 2: Run and confirm they fail**

Run: `python -m pytest tests/test_pending_guard.py -v`
Expected: collection ERROR — `ImportError: cannot import name 'pending_tool_uses'`.

- [ ] **Step 3: Implement**
  1. `friday_agent/core/state.py` — append:

     ```python
     # ---------------------------------------------------------------------------
     # PendingToolUseError — refusing to send an unpaired tool_use
     # ---------------------------------------------------------------------------
     class PendingToolUseError(ValueError):
         """Raised when a state with unanswered tool_use blocks would be sent to the model.

         Such a state is a suspended turn waiting for external results: attach them
         with resume() (an is_error result closes a call that will never finish)
         before calling step() or compact(). A ValueError — a caller-side invalid
         state, never an LLMError / model_error.
         """

         def __init__(self, tool_use_ids: list[str]) -> None:
             self.tool_use_ids = tool_use_ids
             super().__init__(
                 f"state has tool_use blocks without a tool_result: {tool_use_ids}; "
                 "attach their results with resume() first"
             )
     ```

  2. `friday_agent/core/loop.py` — change `from friday_agent.core.state import LoopState, Terminal` to `from friday_agent.core.state import LoopState, PendingToolUseError, Terminal`. Add after `_extract_tool_use_blocks`:

     ```python
     def _last_assistant_index(messages: list[Message]) -> int | None:
         """Index of the last assistant message, or None if there is none."""
         for i in range(len(messages) - 1, -1, -1):
             if messages[i].role == "assistant":
                 return i
         return None


     def pending_tool_uses(state: LoopState) -> list[ContentBlock]:
         """Return the tool_use blocks still waiting for a result.

         Only the last assistant message can hold them (any earlier one was answered
         before the next model call), so pending calls are recomputed from history
         alone — LoopState needs no extra field, and a deserialized state gives the
         same answer. Pure function: no provider, no tools.
         """
         last = _last_assistant_index(state.messages)
         if last is None:
             return []
         answered = {
             block.tool_use_id
             for msg in state.messages[last + 1:]
             for block in msg.content
             if block.type == "tool_result"
         }
         return [
             block
             for block in state.messages[last].content
             if block.type == "tool_use" and block.id not in answered
         ]
     ```

     At the very top of `run_one_turn`'s body (before `config = ...`), add:

     ```python
         # Never send an unpaired tool_use: the API would reject it, and the 400
         # would surface only as an opaque model_error.
         if pending := pending_tool_uses(state):
             raise PendingToolUseError([block.id or "" for block in pending])
     ```

     In `run_one_turn`'s docstring `Raises:`, add first: `PendingToolUseError: when state still has unanswered tool_use blocks (raised before the provider is called).`
  3. `friday_agent/core/engine.py` — change `from friday_agent.core.loop import run_one_turn` to `from friday_agent.core.loop import pending_tool_uses, run_one_turn`, and `from friday_agent.core.state import LoopState, Terminal` to `from friday_agent.core.state import LoopState, PendingToolUseError, Terminal`. At the very top of `step()`'s body and of `compact()`'s body, add:

     ```python
             if pending := pending_tool_uses(state):
                 raise PendingToolUseError([block.id or "" for block in pending])
     ```

     In `step()`'s docstring `Raises:`, add first: `PendingToolUseError: the state still has unanswered tool_use blocks — checked before anything else (no section rendered, no request sent).` In `compact()`'s docstring, add `Raises: PendingToolUseError: the state still has unanswered tool_use blocks (its summary call would send them unpaired).`

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_pending_guard.py -v` → Expected: all PASS.
Run: `python -m pytest -q` → Expected: 286 passed.

- [ ] **Step 5: Update the docs**
  - `docs/architecture/01-core-loop.md`
    - ② table: the `core/loop.py` row becomes `| `friday_agent/core/loop.py` | Single-turn execution · stop_reason branching · pending-call guard | `run_one_turn()`, `pending_tool_uses()` |`; the `core/state.py` key symbols become `Terminal`, `LoopState` (`to_dict`/`from_dict`), `PendingToolUseError`.
    - ③ Execution Order: insert a first step before `1. api_input_messages = …`: `0. pending_tool_uses(state) non-empty → raise PendingToolUseError (no request)`.
    - ③ Branch table: add the row `| State has unanswered `tool_use` (pending) | **raises `PendingToolUseError`** before any request — `step()` and `compact()` alike |`.
    - ④: after the `engine.compact` subsection, add:

      ```
      ### Pending Calls — `pending_tool_uses(state)` / `PendingToolUseError`

      `pending_tool_uses(state)` (`core/loop.py`) returns the `tool_use` blocks of the last assistant message that have no `tool_result` after it. It is a pure function of history, so it gives the same answer for a deserialized state. While it is non-empty, `step()` and `compact()` raise `PendingToolUseError(tool_use_ids)` (a `ValueError`, not an `LLMError`) before doing anything — even when a user message was appended after the unanswered calls. Previously such a state reached the API and came back as a 400 `model_error`.
      ```

  - `docs/architecture/04-context-compaction.md` — in ④ under `engine.compact(state)`, add the bullet: `- Raises `PendingToolUseError` when the state still has unanswered `tool_use` blocks (a suspended turn) — attach them with `resume()` first; the summary call would otherwise send the unpaired `tool_use`.`
  - `docs/architecture/07-data-models.md` — at the end of the "Exception hierarchy" paragraph, add: ` `PendingToolUseError` (`core/state.py`) is deliberately **not** an `LLMError`: a `ValueError` raised by `step()`/`compact()` when the state still has unanswered `tool_use` blocks ([01-core-loop](01-core-loop.md)).`

- [ ] **Step 6: Commit**

```bash
git rev-parse --abbrev-ref HEAD
git add friday_agent/core/state.py friday_agent/core/loop.py friday_agent/core/engine.py tests/test_pending_guard.py docs/architecture/01-core-loop.md docs/architecture/04-context-compaction.md docs/architecture/07-data-models.md
git commit -F - -- friday_agent/core/state.py friday_agent/core/loop.py friday_agent/core/engine.py tests/test_pending_guard.py docs/architecture/01-core-loop.md docs/architecture/04-context-compaction.md docs/architecture/07-data-models.md <<'EOF'
feat(core): refuse to send an unpaired tool_use (PendingToolUseError)

pending_tool_uses(state) recomputes unanswered calls from history.
step(), compact() and run_one_turn() raise PendingToolUseError before any
request while one remains, instead of letting the API's 400 surface as an
opaque model_error.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JRQLSczEWcmgFp8vJsUs9q
EOF
git show --stat HEAD
```

---

### Task 8: Deferred tools — `Tool.is_deferred`, `Suspended`, `resume()`

**Files:**
- Modify: `friday_agent/tools/base.py` (`Tool.is_deferred`)
- Modify: `friday_agent/tools/orchestrator.py` (`_validated_input`, refactor `_is_concurrency_safe`, add `is_deferred_call`, drop the unused `ValidationError` import)
- Modify: `friday_agent/core/state.py` (`Suspended`, `ContentBlock` import, docstrings)
- Modify: `friday_agent/core/loop.py` (module docstring, imports, deferred split in `run_one_turn`, `_answers`, `resume`)
- Modify: `friday_agent/core/engine.py` (imports, `step` type and docstring)
- Modify: `tests/_drive.py`
- Create: `tests/test_deferred_tools.py`
- Modify: `docs/architecture/01-core-loop.md`, `02-tool-orchestration.md`, `07-data-models.md`, `00-overview.md`, `CLAUDE.md`, `README.md`

**Interfaces:**
- Consumes: `pending_tool_uses`, `_last_assistant_index` (Task 7); `to_tool_result_message` (Task 5); `apply_state_effects`, `_extract_tool_use_blocks` (existing in `loop.py`).
- Produces: `Tool.is_deferred(self, input: dict) -> bool`; `is_deferred_call(block: ContentBlock, tools: list[Tool]) -> bool`; `@dataclass Suspended(state: LoopState, pending: list[ContentBlock])`; `resume(state: LoopState, results: dict[str, ToolResult]) -> LoopState`; `step()` final sentinel `LoopState | Suspended | Terminal`.

- [ ] **Step 1: Write the failing tests** — create `tests/test_deferred_tools.py`:

```python
"""Deferred tools — Tool.is_deferred, Suspended, resume()."""
import json

import pytest
from pydantic import BaseModel, Field

from friday_agent.api.provider import AssistantResponse, StopReason, TextBlock, TokenUsage, ToolUseBlock
from friday_agent.core.engine import FridayAgent
from friday_agent.core.loop import pending_tool_uses, resume
from friday_agent.core.state import LoopState, PendingToolUseError, Suspended, Terminal
from friday_agent.messages.types import create_user_message
from friday_agent.tools.base import Tool, ToolResult
from friday_agent.tools.builtin.example_tool import ExampleTool
from tests._drive import collect_turn
from tests.fakes import FakeLLMProvider


class ApprovalInput(BaseModel):
    action: str = Field(description="What needs approval")


class Approval(Tool):
    """Asks a human to approve an action; the answer arrives later."""

    name = "approval"

    def __init__(self) -> None:
        self.calls = 0

    def input_schema(self) -> type[BaseModel]:
        return ApprovalInput

    def is_deferred(self, input: dict) -> bool:
        return True

    async def call(self, args: dict) -> ToolResult:
        self.calls += 1
        return ToolResult(data="approval runs outside step()", is_error=True)


def _calls(*blocks: ToolUseBlock) -> AssistantResponse:
    return AssistantResponse(content=list(blocks), stop_reason=StopReason.TOOL_USE, usage=TokenUsage())


def _approval(tool_id: str) -> ToolUseBlock:
    return ToolUseBlock(id=tool_id, name="approval", input={"action": "send"})


def _example(tool_id: str) -> ToolUseBlock:
    return ToolUseBlock(id=tool_id, name="ExampleTool", input={"payload": "x"})


def _text(text: str = "done") -> AssistantResponse:
    return AssistantResponse(content=[TextBlock(text=text)], stop_reason=StopReason.END_TURN, usage=TokenUsage())


def _result_ids(state: LoopState) -> list[str]:
    return [b.tool_use_id for m in state.messages for b in m.content if b.type == "tool_result"]


def _start() -> LoopState:
    return LoopState(messages=[create_user_message("go")])


@pytest.mark.asyncio
async def test_deferred_only_call_suspends_without_running():
    tool = Approval()
    fake = FakeLLMProvider(responses=[_calls(_approval("a1"))])

    messages, outcome = await collect_turn(FridayAgent(provider=fake, tools=[tool]), _start())

    assert isinstance(outcome, Suspended)
    assert tool.calls == 0
    assert [b.id for b in outcome.pending] == ["a1"]
    assert [m.type for m in messages] == ["assistant"]  # no tool_result yielded
    assert outcome.state.turn_count == 2
    restored = LoopState.from_dict(json.loads(json.dumps(outcome.state.to_dict())))
    assert restored.to_dict() == outcome.state.to_dict()
    assert [b.id for b in pending_tool_uses(restored)] == ["a1"]


@pytest.mark.asyncio
async def test_mixed_response_runs_normal_tool_and_holds_deferred():
    fake = FakeLLMProvider(responses=[_calls(_approval("a1"), _example("e1"))])

    _, outcome = await collect_turn(FridayAgent(provider=fake, tools=[Approval(), ExampleTool()]), _start())

    assert isinstance(outcome, Suspended)
    assert _result_ids(outcome.state) == ["e1"]
    assert [b.id for b in outcome.pending] == ["a1"]


@pytest.mark.asyncio
async def test_full_resume_orders_results_and_step_continues():
    fake = FakeLLMProvider(responses=[_calls(_approval("a1"), _example("e1")), _text("approved and done")])
    engine = FridayAgent(provider=fake, tools=[Approval(), ExampleTool()])
    _, suspended = await collect_turn(engine, _start())

    state = resume(suspended.state, {"a1": ToolResult(data="approved")})

    assert _result_ids(state) == ["a1", "e1"]  # tool_use order, not arrival order
    assert pending_tool_uses(state) == []
    assert state.turn_count == suspended.state.turn_count
    _, outcome = await collect_turn(engine, state)
    assert isinstance(outcome, Terminal) and outcome.reason == "completed"
    sent = fake.received_messages[1]
    assert [b["tool_use_id"] for m in sent for b in m["content"] if b["type"] == "tool_result"] == ["a1", "e1"]


@pytest.mark.asyncio
async def test_partial_resume_keeps_rest_pending():
    fake = FakeLLMProvider(responses=[_calls(_approval("a1"), _approval("a2"))])
    engine = FridayAgent(provider=fake, tools=[Approval()])
    _, suspended = await collect_turn(engine, _start())

    state = resume(suspended.state, {"a2": ToolResult(data="ok")})

    assert [b.id for b in pending_tool_uses(state)] == ["a1"]
    with pytest.raises(PendingToolUseError) as exc:
        await collect_turn(engine, state)
    assert exc.value.tool_use_ids == ["a1"]
    assert fake.call_count == 1


@pytest.mark.asyncio
async def test_cancel_flow_takes_one_model_call():
    fake = FakeLLMProvider(responses=[_calls(_approval("a1")), _text("ok, cancelled")])
    engine = FridayAgent(provider=fake, tools=[Approval()])
    _, suspended = await collect_turn(engine, _start())

    state = resume(suspended.state, {"a1": ToolResult(data="cancelled by the user", is_error=True)})
    state.messages.append(create_user_message("never mind, stop"))
    _, outcome = await collect_turn(engine, state)

    assert outcome.reason == "completed"
    assert fake.call_count == 2
    assert [m["role"] for m in fake.received_messages[1]] == ["user", "assistant", "user", "user"]


@pytest.mark.asyncio
async def test_resume_rejects_unknown_and_already_answered_ids():
    fake = FakeLLMProvider(responses=[_calls(_approval("a1"))])
    _, suspended = await collect_turn(FridayAgent(provider=fake, tools=[Approval()]), _start())

    with pytest.raises(ValueError, match="nope"):
        resume(suspended.state, {"nope": ToolResult(data="x")})
    answered = resume(suspended.state, {"a1": ToolResult(data="x")})
    with pytest.raises(ValueError, match="a1"):
        resume(answered, {"a1": ToolResult(data="again")})


@pytest.mark.asyncio
async def test_resume_inserts_results_ahead_of_appended_user_message():
    fake = FakeLLMProvider(responses=[_calls(_approval("a1")), _text()])
    engine = FridayAgent(provider=fake, tools=[Approval()])
    _, suspended = await collect_turn(engine, _start())
    early = suspended.state
    early.messages.append(create_user_message("are you there?"))

    with pytest.raises(PendingToolUseError):
        await collect_turn(engine, early)
    state = resume(early, {"a1": ToolResult(data="approved")})

    assert [m.content[0].type for m in state.messages] == ["text", "tool_use", "tool_result", "text"]
    _, outcome = await collect_turn(engine, state)
    assert outcome.reason == "completed"


@pytest.mark.asyncio
async def test_resume_applies_state_effect():
    fake = FakeLLMProvider(responses=[_calls(_approval("a1"))])
    _, suspended = await collect_turn(FridayAgent(provider=fake, tools=[Approval()]), _start())
    todos = [{"content": "ship", "status": "completed"}]

    state = resume(suspended.state, {"a1": ToolResult(data="ok", state_effect={"todos": todos})})

    assert state.todos == todos


@pytest.mark.asyncio
async def test_resume_does_not_mutate_input_and_empty_results_is_noop():
    fake = FakeLLMProvider(responses=[_calls(_approval("a1"))])
    _, suspended = await collect_turn(FridayAgent(provider=fake, tools=[Approval()]), _start())
    before = suspended.state.to_dict()

    resume(suspended.state, {"a1": ToolResult(data="ok")})

    assert suspended.state.to_dict() == before
    assert resume(suspended.state, {}).to_dict() == before


@pytest.mark.asyncio
async def test_invalid_input_to_deferred_tool_runs_inline_with_error():
    tool = Approval()
    bad = ToolUseBlock(id="a1", name="approval", input={"wrong": 1})
    fake = FakeLLMProvider(responses=[_calls(bad)])

    _, outcome = await collect_turn(FridayAgent(provider=fake, tools=[tool]), _start())

    assert isinstance(outcome, LoopState)  # not deferred: no Suspended
    assert tool.calls == 1
    assert outcome.messages[-1].content[0].is_error


@pytest.mark.asyncio
async def test_unknown_tool_beside_deferred_errors_immediately():
    unknown = ToolUseBlock(id="u1", name="nope", input={})
    fake = FakeLLMProvider(responses=[_calls(_approval("a1"), unknown)])

    _, outcome = await collect_turn(FridayAgent(provider=fake, tools=[Approval()]), _start())

    assert isinstance(outcome, Suspended)
    assert _result_ids(outcome.state) == ["u1"]
    assert [b.id for b in outcome.pending] == ["a1"]


@pytest.mark.asyncio
async def test_callers_without_deferred_tools_never_see_suspended():
    fake = FakeLLMProvider(responses=[_calls(_example("e1")), _text()])
    engine = FridayAgent(provider=fake, tools=[ExampleTool()])

    _, first = await collect_turn(engine, _start())
    _, second = await collect_turn(engine, first)

    assert type(first) is LoopState and isinstance(second, Terminal)
```

- [ ] **Step 2: Run and confirm they fail**

Run: `python -m pytest tests/test_deferred_tools.py -v`
Expected: collection ERROR — `ImportError: cannot import name 'resume'` (or `'Suspended'`).

- [ ] **Step 3: `Tool.is_deferred`** — in `friday_agent/tools/base.py`, after `is_concurrency_safe`, add:

```python
    def is_deferred(self, input: dict) -> bool:
        """Return whether this call's result arrives later, outside step(). Defaults to False.

        A deferred call is not executed by step(): the turn ends with
        Suspended(state, pending) and the caller attaches the result later with
        resume(). call() still runs for calls this returns False for — including
        calls whose input fails schema validation, which are never deferred.
        """
        return False
```

- [ ] **Step 4: `is_deferred_call`** — in `friday_agent/tools/orchestrator.py`, delete `from pydantic import ValidationError`, then replace `_is_concurrency_safe` with:

```python
def _validated_input(tool: Tool, block: ContentBlock) -> dict | None:
    """Return the block input validated against the tool's schema, or None if it does not validate."""
    if block.input is None:
        return None
    try:
        return tool.input_schema().model_validate(block.input).model_dump()
    except Exception:
        return None


def _is_concurrency_safe(tool: Tool, block: ContentBlock) -> bool:
    """Return whether a single ContentBlock is concurrency-safe.

    Validates the block input against the tool's schema first; None or invalid
    input returns False (conservative fallback). On success, delegates to
    ``tool.is_concurrency_safe``; a raising predicate also counts as False.
    """
    parsed = _validated_input(tool, block)
    if parsed is None:
        return False
    try:
        return bool(tool.is_concurrency_safe(parsed))
    except Exception:
        return False


def is_deferred_call(block: ContentBlock, tools: list[Tool]) -> bool:
    """Return whether a tool_use block is held back for an external result.

    Evaluated like concurrency safety: an unknown tool, input that fails schema
    validation, and a raising predicate all count as not deferred — such a call
    runs inline and the model gets an immediate error, instead of an external
    executor receiving a malformed call.
    """
    tool = _find_tool(tools, block.name or "")
    if tool is None:
        return False
    parsed = _validated_input(tool, block)
    if parsed is None:
        return False
    try:
        return bool(tool.is_deferred(parsed))
    except Exception:
        return False
```

- [ ] **Step 5: `Suspended`** — in `friday_agent/core/state.py`:
  1. Change `from friday_agent.messages.types import Message` to `from friday_agent.messages.types import ContentBlock, Message`.
  2. Module docstring: replace `Terminal (loop exit) and LoopState — ...` with `Terminal (loop exit), Suspended (paused on deferred tool calls), and LoopState — the serializable loop state, which also serves as the "continue" sentinel and the transport unit for distributed resume.`
  3. Insert between `LoopState` and `PendingToolUseError`:

     ```python
     # ---------------------------------------------------------------------------
     # Suspended — the turn paused on deferred tool calls
     # ---------------------------------------------------------------------------
     @dataclass
     class Suspended:
         """Returned when a turn ends waiting on deferred tool calls.

         state   — the state to persist: input messages + the assistant message +
                   results of the calls that ran; turn_count/todos advanced as in
                   any tool turn. Serialize it like any LoopState.
         pending — the deferred tool_use blocks (tool_use order) whose results the
                   caller produces elsewhere and attaches with resume(). Recomputable
                   from state alone via pending_tool_uses(), so it is not serialized.
         """
         state: LoopState
         pending: list[ContentBlock]
     ```

- [ ] **Step 6: Deferred split and `resume`** — in `friday_agent/core/loop.py`:
  1. Replace the module docstring's last paragraph (from `A turn ends by yielding`) with:

     ```
     A turn ends by yielding exactly one sentinel, each carrying the state to
     persist: the next LoopState (continue), Suspended (the response called
     deferred tools; the other calls ran, and the deferred results arrive later via
     resume()), or Terminal (done). The caller drives the turn loop by calling
     run_one_turn() in a while-true — there is no batch driver and no internal
     compaction. pending_tool_uses() and resume() are pure state functions (no
     provider, no tools), so any process can attach late results.
     ```

  2. Imports: `from friday_agent.core.state import LoopState, PendingToolUseError, Suspended, Terminal`; `from friday_agent.tools.base import Tool, ToolResult`; `from friday_agent.tools.orchestrator import is_deferred_call, run_tools, to_tool_result_message`.
  3. In `run_one_turn`: return annotation becomes `AsyncGenerator[Message | LoopState | Suspended | Terminal, None]`. In the docstring, replace the sentinel list with:

     ```
           - LoopState: loop continues — the updated state for the next turn.
           - Suspended: the response called deferred tools; the other calls ran, and
             Suspended.state waits for the deferred results (attach with resume()).
           - Terminal: loop ends (completed / model_error); Terminal.state is the
             state to persist.
     ```

     Replace everything from `# Execute all tool_use blocks, collecting results and declarative state effects.` to the end of the function with:

```python
    # Deferred calls wait for an external result; every other call runs now.
    held = [is_deferred_call(block, tools) for block in tool_use_blocks]
    deferred = [block for block, h in zip(tool_use_blocks, held) if h]
    immediate = [block for block, h in zip(tool_use_blocks, held) if not h]

    effects: list[dict] = []
    tool_results: list[Message] = []
    async for result_msg in run_tools(
        immediate, tools, max_concurrency=max_concurrency, effects_sink=effects
    ):
        tool_results.append(result_msg)
        yield result_msg

    # Assemble the next state from the CLEAN state.messages (NOT
    # api_input_messages) so the turn-local reminders are never persisted.
    next_state = LoopState(
        messages=[*state.messages, message, *tool_results],
        turn_count=state.turn_count + 1,
        todos=apply_state_effects(state.todos, effects),
    )
    yield Suspended(state=next_state, pending=deferred) if deferred else next_state
```

  4. Append at the end of the module:

```python
def _answers(msg: Message, order: dict[str, int]) -> bool:
    """Whether msg is a tool_result-only message answering a call in `order`."""
    return bool(msg.content) and all(
        block.type == "tool_result" and block.tool_use_id in order for block in msg.content
    )


def resume(state: LoopState, results: dict[str, ToolResult]) -> LoopState:
    """Attach externally produced results to a suspended state. Never calls the model.

    Each result becomes a tool_result message through the same conversion
    run_tools uses (to_tool_result_message — errors and images behave
    identically), and state_effects are applied in tool_use order. All results
    for the last assistant message end up in tool_use order, ahead of any other
    trailing message (tool results must precede text in a user turn — this also
    repairs a state that got a user message appended before resume). Partial
    results are allowed: the rest stay pending, and step() keeps refusing the
    state until they are attached. Returns a new state; the input is untouched,
    and turn_count is unchanged (the turn was counted when it suspended).

    Raises:
        ValueError: a key is not a pending tool_use id (unknown or already answered).
    """
    pending_ids = {block.id for block in pending_tool_uses(state)}
    invalid = [tool_use_id for tool_use_id in results if tool_use_id not in pending_ids]
    if invalid:
        raise ValueError(f"resume: not pending (unknown or already answered): {invalid}")
    if not results:
        return LoopState(messages=list(state.messages), turn_count=state.turn_count, todos=state.todos)

    last = _last_assistant_index(state.messages)  # set: results are non-empty and all pending
    order = {block.id: n for n, block in enumerate(_extract_tool_use_blocks(state.messages[last]))}
    ids = sorted(results, key=order.__getitem__)
    trailing = state.messages[last + 1:]
    answers = [msg for msg in trailing if _answers(msg, order)]
    answers += [to_tool_result_message(tool_use_id, results[tool_use_id]) for tool_use_id in ids]
    answers.sort(key=lambda msg: order[msg.content[0].tool_use_id])
    others = [msg for msg in trailing if not _answers(msg, order)]
    effects = [results[i].state_effect for i in ids if results[i].state_effect is not None]
    return LoopState(
        messages=[*state.messages[: last + 1], *answers, *others],
        turn_count=state.turn_count,
        todos=apply_state_effects(state.todos, effects),
    )
```

- [ ] **Step 7: Engine and driver**
  1. `friday_agent/core/engine.py`: `from friday_agent.core.state import LoopState, PendingToolUseError, Suspended, Terminal`. The `step` signature becomes `async def step(self, state: LoopState) -> AsyncGenerator[Message | LoopState | Suspended | Terminal, None]:`. In its docstring, replace `then yields exactly one final sentinel: the next LoopState (loop may continue) or a Terminal (loop ended).` with `then yields exactly one final sentinel: the next LoopState (continue), Suspended (paused on deferred tool calls — attach their results with resume(), then step() again), or a Terminal (ended; terminal.state is the state to keep).` In the module docstring, replace `the next LoopState (loop may continue) or Terminal (loop has ended)` with `the next LoopState (continue), Suspended (paused on deferred tool calls) or Terminal (ended)`.
  2. `tests/_drive.py`: import `Suspended` too (`from friday_agent.core.state import LoopState, Suspended, Terminal`). In `drive`, the yield annotation becomes `AsyncGenerator[Message | Terminal | Suspended, None]`, the docstring's last sentence becomes `Yields every Message produced across turns, then the final Terminal — or the Suspended that paused the run on a deferred tool call.`, the outcome annotation includes `Suspended`, and `if isinstance(outcome, Terminal):` becomes `if isinstance(outcome, (Terminal, Suspended)):`. In `collect_turn`, the return annotation becomes `"tuple[list[Message], LoopState | Suspended | Terminal]"`, `isinstance(item, (LoopState, Terminal))` becomes `isinstance(item, (LoopState, Suspended, Terminal))`, and the assert message becomes `"step() must yield a LoopState, Suspended or Terminal sentinel"`. In `drive`'s loop, change the sentinel check to `isinstance(item, (LoopState, Suspended, Terminal))`.

- [ ] **Step 8: Run the tests**

Run: `python -m pytest tests/test_deferred_tools.py tests/test_partition.py tests/test_run_tools.py -v` → Expected: all PASS.
Run: `python -m pytest -q` → Expected: 298 passed.

- [ ] **Step 9: Update the docs**
  - `docs/architecture/01-core-loop.md`
    - ② table: the `core/loop.py` row becomes `| `friday_agent/core/loop.py` | Single-turn execution · stop_reason branching · deferred calls · pending-call guard · resume | `run_one_turn()`, `pending_tool_uses()`, `resume()` |`; `core/state.py` symbols become `Terminal`, `Suspended`, `LoopState` (`to_dict`/`from_dict`), `PendingToolUseError`.
    - ③ Execution Order: replace steps 3–4 with

      ```
      3. if there are tool_use blocks
            ├─ deferred calls (Tool.is_deferred → True) are held back
            └─ run_tools(the rest, effects_sink=effects) ← parallel tool execution + state_effect collection
                  └─ yield tool_result message (each result)

      4. end of turn: yield 1 sentinel (next_todos = apply_state_effects(state.todos, effects))
            LoopState              ─ loop continues (clean state.messages + next_todos)
            Suspended              ─ paused on deferred calls: .state (to persist) + .pending (tool_use blocks)
            Terminal               ─ loop terminates; .state = the state to persist
      ```

    - ③ Branch table: add `| tool_use includes deferred calls (`Tool.is_deferred`) | the other calls run first, then `Suspended(state, pending)` — `state.turn_count+1` |`.
    - ④ `engine.step`: in the intro sentence, `(`LoopState` or `Terminal`)` becomes `(`LoopState`, `Suspended` or `Terminal`)`. Replace the example with:

      ```python
      async for item in engine.step(state):
          if isinstance(item, (LoopState, Suspended, Terminal)):
              outcome = item        # LoopState → next turn (use outcome as state as-is)
                                    # Suspended → persist outcome.state; run outcome.pending elsewhere
                                    # Terminal  → loop terminates (outcome.state = state to keep)
          else:
              render(item)          # Message: assistant response or tool_result — consumable on arrival
      ```

    - ④: after the "Pending Calls" subsection, add:

      ````
      ### Deferred Tools — `Suspended` → `resume(state, results)`

      A tool whose `is_deferred(input)` returns `True` is not executed by `step()`. The other calls in the response run as usual, then the turn ends with `Suspended(state, pending)`. The caller persists `state` (an ordinary `LoopState`), hands `pending` (the deferred `tool_use` blocks) to whoever produces the results — a person, another service, a job queue — and finishes. When results arrive, possibly in another process:

      ```python
      state = LoopState.from_dict(load())
      pending_tool_uses(state)                         # the calls still waiting
      state = resume(state, {tool_use_id: ToolResult(data="approved")})
      async for item in engine.step(state): ...        # the next turn, as usual
      ```

      `resume()` is a pure function: no provider, no tools, no model call. It converts each result with `to_tool_result_message()` (the same conversion `run_tools` uses), inserts it in `tool_use` order ahead of any other trailing message, applies `state_effect`s, and keeps `turn_count`. Partial results are allowed (the rest stay pending); an id that is not pending raises `ValueError`. To close a call that will never finish (cancel, timeout, superseding instruction), pass an `is_error=True` result, append the new user message, and call `step()`.
      ````

    - ④ State Types table: add `| `Suspended(state, pending)` | `core/state.py` | "Paused on deferred tool calls" sentinel; persist `state`, `pending` is recomputable via `pending_tool_uses()` |`.
    - ⑦: append

      ```
      **Why is `resume()` a pure function, not an engine method?**
      Attaching a result needs neither the provider (credentials) nor the tools, so the process that receives an external result — a webhook, a queue worker — can attach it and persist the state without building an agent. `step()` remains the single entry point that runs a turn.
      ```

  - `docs/architecture/02-tool-orchestration.md`
    - ② table: add `is_deferred_call()` to the `tools/orchestrator.py` symbols.
    - ③: after the "concurrency-safe determination" paragraph, add: `**Deferred calls**: before partitioning, `run_one_turn` holds back every call for which `is_deferred_call()` is true — the tool's `is_deferred(input)` after the same conservative checks (unknown tool, `None` or invalid input, or a raising predicate → not deferred, so the call runs inline and the model gets an immediate error). Only the remaining calls are partitioned and run; the deferred ones end the turn as `Suspended` ([01-core-loop](01-core-loop.md)).`
    - ④ Tool methods table: add `| `is_deferred(input)` | `bool` | `False` | Whether the call's result arrives later, outside `step()` (the turn ends with `Suspended`) |`.
    - ⑥: add `**`is_deferred` conservative default** — `False` unless overridden. A deferred tool's `call()` still runs for calls that are not deferred (for example, invalid input), so make it return a clear error result in that case.`
  - `docs/architecture/07-data-models.md`
    - ① table: in the `LoopState` row's transport-role cell, append ` — persisted as-is, or as `Suspended.state` / `Terminal.state``. Below the table, add: `> `Suspended` and `Terminal` are not serialized themselves — persist their `.state`. `Suspended.pending` is recomputable from that state via `pending_tool_uses()`.`
    - 2.2: after the `Terminal` section, add

      ```
      #### `Suspended` — the "paused on deferred tool calls" sentinel

      | Field | Type | Meaning |
      |---|---|---|
      | `state` | `LoopState` | State to persist: input + assistant message + results of the calls that ran (`turn_count+1`, todos updated) |
      | `pending` | `list[ContentBlock]` | Deferred `tool_use` blocks in `tool_use` order — recomputable via `pending_tool_uses(state)`, so not serialized |
      ```

    - ③ transport flow: replace `   LoopState(messages=[Message], turn_count)` with `   LoopState(messages=[Message], turn_count, todos)   ← itself, Suspended.state or Terminal.state`.
  - `docs/architecture/00-overview.md` — big-picture block: replace `   └─ yield: LoopState | Terminal      ← exactly 1 final sentinel` with `   └─ yield: LoopState | Suspended | Terminal   ← exactly 1 final sentinel (each carries the state to persist)`, and `If item is LoopState, call step() again with it as-is; if Terminal, stop.` with `If item is LoopState, call step() again with it as-is; if Suspended, persist item.state and attach the deferred results later with resume(); if Terminal, stop (item.state is the state to keep).`
  - `CLAUDE.md`
    - Diagram: replace `        │                │ → lastly yield one LoopState | Terminal        │` with `        │                │ → lastly yield LoopState | Suspended | Terminal│` (keep the box aligned), and the line `Caller consumes via `async for`: if the last sentinel is a LoopState, call step() again with it as-is; if Terminal, stop.` with `Caller consumes via `async for`: LoopState → call step() again with it as-is; Suspended → persist .state, attach the deferred results with resume(), then step(); Terminal → stop (.state is the state to keep).`
    - Core data structures: after the **LoopState** bullet, add `- **Suspended** — the turn paused on deferred tool calls (`Tool.is_deferred`): `.state` to persist + `.pending` tool_use blocks. Results are attached by the pure function `resume(state, results)` (`core/loop.py`; no provider needed), then `step()` continues; `pending_tool_uses(state)` recomputes what is still waiting.`
    - Public API sentence: replace `then finally yields one `LoopState | Terminal` sentinel)` with `then finally yields one `LoopState | Suspended | Terminal` sentinel)`, and after `+ `engine.compact(state)` (caller-driven compact)` insert ` + pure state functions `pending_tool_uses(state)` / `resume(state, results)` (`core/loop.py`)`.
    - Pitfalls: add `- A state whose last assistant message still has unanswered `tool_use` blocks (a suspended turn) is refused by `step()`/`compact()` with `PendingToolUseError` before any request — attach the results with `resume()` first (an `is_error` result closes a call that will never finish).`
  - `README.md` — in `#### Execution Policy Methods Are Not Sent to the LLM`, replace `` `is_concurrency_safe` is **not included** in`` with `` `is_concurrency_safe` and `is_deferred` are **not included** in``, and add the quote line `> A tool whose `is_deferred()` is `True` is not run by `step()` at all — its result arrives later (see [Deferred Tools](#deferred-tools-results-that-arrive-later)).` At the end of `## Distributed Resume (stateless)`, add:

    ````markdown
    ### Deferred Tools (results that arrive later)

    Some results cannot be produced inside `step()` — a human approval, a job that runs elsewhere. Mark the tool deferred and the turn pauses instead of waiting:

    ```python
    from friday_agent.core.loop import pending_tool_uses, resume
    from friday_agent.core.state import Suspended

    class SendEmail(Tool):
        ...
        def is_deferred(self, input: dict) -> bool:
            return True                                   # step() will not run it

    # Request handler — the turn ends with Suspended
    async for item in engine.step(state):
        outcome = item
    if isinstance(outcome, Suspended):
        save(json.dumps(outcome.state.to_dict()))         # other tools' results are already in it
        for call in outcome.pending:                      # deferred tool_use blocks (id, name, input)
            request_approval(call.id, call.input)

    # Hours later, any process (no provider needed)
    state = LoopState.from_dict(json.loads(load()))
    state = resume(state, {call_id: ToolResult(data="Sent.")})   # or ToolResult(..., is_error=True) to cancel
    save(json.dumps(state.to_dict()))                     # then run the next turn with engine.step(state)
    ```

    Calling `step()` on a state that still has unanswered calls raises `PendingToolUseError` before any request.
    ````

- [ ] **Step 10: Commit**

```bash
git rev-parse --abbrev-ref HEAD
git add friday_agent/tools/base.py friday_agent/tools/orchestrator.py friday_agent/core/state.py friday_agent/core/loop.py friday_agent/core/engine.py tests/_drive.py tests/test_deferred_tools.py docs/architecture/01-core-loop.md docs/architecture/02-tool-orchestration.md docs/architecture/07-data-models.md docs/architecture/00-overview.md CLAUDE.md README.md
git commit -F - -- friday_agent/tools/base.py friday_agent/tools/orchestrator.py friday_agent/core/state.py friday_agent/core/loop.py friday_agent/core/engine.py tests/_drive.py tests/test_deferred_tools.py docs/architecture/01-core-loop.md docs/architecture/02-tool-orchestration.md docs/architecture/07-data-models.md docs/architecture/00-overview.md CLAUDE.md README.md <<'EOF'
feat(core): deferred tools — Suspended and resume()

A tool whose is_deferred(input) is true is not run by step(): the other
calls in the response run, and the turn ends with Suspended(state,
pending). resume(state, results) is a pure function that attaches late
results in tool_use order (ahead of other trailing messages) and applies
their state effects; the next turn runs through step() as usual.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JRQLSczEWcmgFp8vJsUs9q
EOF
git show --stat HEAD
```

---

### Task 9: Same-prefix compaction — `compact(reuse_prefix=True)`

**Files:**
- Modify: `friday_agent/context/compact.py` (comment block, imports, `compact_conversation`, new `_response_text` and `_has_tool_use`)
- Modify: `friday_agent/core/engine.py` (imports, `_effective_system_prompt`, `_tool_schemas`, `step`, `compact`)
- Create: `tests/test_compact_reuse_prefix.py`
- Modify: `docs/architecture/04-context-compaction.md`, `01-core-loop.md`, `CLAUDE.md`, `README.md`

**Interfaces:**
- Consumes: `_extract_summary` (Task 1); `pending_tool_uses` and `PendingToolUseError` (Task 7).
- Produces: `compact_conversation(*, provider, messages, extra_instructions="", system_prompt=SUMMARIZER_SYSTEM_PROMPT, tools=None) -> str`; `FridayAgent.compact(state, *, reuse_prefix: bool = False) -> LoopState`.

- [ ] **Step 1: Write the failing tests** — create `tests/test_compact_reuse_prefix.py`:

```python
"""engine.compact(reuse_prefix=True) — the summary call shares step()'s system prompt and tools."""
import pytest

from friday_agent.api.provider import AssistantResponse, StopReason, TextBlock, TokenUsage, ToolUseBlock
from friday_agent.context.compact import COMPACT_PROMPT, SUMMARIZER_SYSTEM_PROMPT, compact_conversation
from friday_agent.core.engine import FridayAgent
from friday_agent.core.state import LoopState
from friday_agent.memory.store import MemoryEntry, MemoryType
from friday_agent.messages.types import create_user_message
from friday_agent.tools.builtin.example_tool import ExampleTool
from tests._drive import collect_turn
from tests.fakes import FakeLLMProvider, InMemoryStore


def _text(text: str) -> AssistantResponse:
    return AssistantResponse(content=[TextBlock(text=text)], stop_reason=StopReason.END_TURN, usage=TokenUsage())


def _summary(body: str = "S") -> AssistantResponse:
    return _text(f"<analysis>a</analysis><summary>{body}</summary>")


async def _engine_after_one_turn(fake: FakeLLMProvider, **kwargs):
    engine = FridayAgent(provider=fake, tools=[ExampleTool()], system_prompt="DOMAIN", **kwargs)
    _, outcome = await collect_turn(engine, LoopState(messages=[create_user_message("hi")]))
    return engine, outcome.state


def _summary_text(state: LoopState) -> str:
    return state.messages[0].content[0].text


@pytest.mark.asyncio
async def test_reuse_prefix_matches_step_system_and_tools():
    fake = FakeLLMProvider(responses=[_text("hello"), _summary()])
    engine, state = await _engine_after_one_turn(fake)

    await engine.compact(state, reuse_prefix=True)

    assert fake.received_system_prompts[1] == fake.received_system_prompts[0]
    assert fake.received_tools[1] == fake.received_tools[0]
    assert fake.received_messages[1][-1]["content"] == COMPACT_PROMPT


@pytest.mark.asyncio
async def test_reuse_prefix_matches_step_with_memory_mounted():
    store = InMemoryStore()
    await store.save(MemoryEntry(name="n", description="d", type=MemoryType.user, body="b"))
    fake = FakeLLMProvider(responses=[_text("hello"), _summary()])
    engine, state = await _engine_after_one_turn(fake, memory=store)

    await engine.compact(state, reuse_prefix=True)

    assert fake.received_system_prompts[1] == fake.received_system_prompts[0]
    assert fake.received_tools[1] == fake.received_tools[0]


@pytest.mark.asyncio
async def test_tool_use_reply_retries_once_without_tools():
    sneaky = AssistantResponse(
        content=[ToolUseBlock(id="x", name="ExampleTool", input={"payload": "p"})],
        stop_reason=StopReason.TOOL_USE,
        usage=TokenUsage(),
    )
    fake = FakeLLMProvider(responses=[_text("hello"), sneaky, _summary("FROM RETRY")])
    engine, state = await _engine_after_one_turn(fake)

    compacted = await engine.compact(state, reuse_prefix=True)

    assert fake.call_count == 3
    assert fake.received_tools[2] == []
    assert fake.received_system_prompts[2] == fake.received_system_prompts[0]
    assert "FROM RETRY" in _summary_text(compacted)


@pytest.mark.asyncio
async def test_missing_summary_retries_once_without_tools():
    fake = FakeLLMProvider(responses=[_text("hello"), _text("no tags at all"), _summary("SECOND")])
    engine, state = await _engine_after_one_turn(fake)

    compacted = await engine.compact(state, reuse_prefix=True)

    assert fake.call_count == 3
    assert "SECOND" in _summary_text(compacted)


@pytest.mark.asyncio
async def test_default_compact_is_unchanged():
    fake = FakeLLMProvider(responses=[_text("hello"), _summary()])
    engine, state = await _engine_after_one_turn(fake)

    await engine.compact(state)

    assert fake.received_system_prompts[1] == SUMMARIZER_SYSTEM_PROMPT
    assert fake.received_tools[1] == []
    assert fake.call_count == 2


@pytest.mark.asyncio
async def test_compact_conversation_default_path_never_retries():
    fake = FakeLLMProvider(responses=[_text("no tags at all")])

    result = await compact_conversation(provider=fake, messages=[{"role": "user", "content": "x"}])

    assert result == "no tags at all"
    assert fake.call_count == 1


@pytest.mark.asyncio
async def test_compact_never_renders_turn_sections():
    rendered: list[int] = []

    async def section(state: LoopState) -> str:
        rendered.append(1)
        return "PER-TURN"

    fake = FakeLLMProvider(responses=[_text("hello"), _summary(), _summary()])
    engine, state = await _engine_after_one_turn(fake, turn_sections=[section])
    rendered.clear()

    await engine.compact(state)
    await engine.compact(state, reuse_prefix=True)

    assert rendered == []
    for call in (1, 2):
        assert "PER-TURN" not in str(fake.received_messages[call])
```

- [ ] **Step 2: Run and confirm they fail**

Run: `python -m pytest tests/test_compact_reuse_prefix.py -v`
Expected: FAIL — `TypeError: FridayAgent.compact() got an unexpected keyword argument 'reuse_prefix'`. (`test_default_compact_is_unchanged` and `test_compact_conversation_default_path_never_retries` already pass.)

- [ ] **Step 3: `compact_conversation`** — in `friday_agent/context/compact.py`:
  1. Imports: `from friday_agent.api.provider import AssistantResponse, LLMProvider, ToolUseBlock`.
  2. In the comment block above `_COMPACT_PROMPT_HEAD`, replace the second paragraph (`The no-tools guard appears ONCE ...`) with:

     ```python
     # The no-tools guard appears ONCE, in the CRITICAL line. By default
     # compact_conversation calls complete() with tools=[] and both adapters omit the
     # field entirely when empty, so the model cannot emit a tool_use block at all.
     # A caller reusing the agent's prefix (engine.compact(reuse_prefix=True)) sends
     # the agent's tools; there the CRITICAL line is the request, and a tool_use reply
     # is retried once with tools=[] (see compact_conversation).
     ```

  3. Replace the whole `compact_conversation` function — keep `_extract_summary` from Task 1 below it — with:

```python
async def compact_conversation(
    *,
    provider: LLMProvider,
    messages: list[dict],
    extra_instructions: str = "",
    system_prompt: str = SUMMARIZER_SYSTEM_PROMPT,
    tools: list[dict] | None = None,
) -> str:
    """Summarise a conversation and return the extracted summary text.

    By default the call runs under SUMMARIZER_SYSTEM_PROMPT with tools=[] (no
    tool call possible). A caller reusing the agent's own prefix passes its
    system_prompt and tools so the provider can serve the history from cache;
    if that reply contains a tool_use or no usable <summary>, the call is retried
    once with tools=[] (same system prompt). The <analysis> block is discarded;
    without a usable <summary> pair (see _extract_summary) the entire response
    text is returned as a graceful fallback.

    Args:
        provider: LLM backend used to generate the summary.
        messages: Conversation history in API-ready ``list[dict]`` form.
        extra_instructions: Domain summary requirements folded into the compact
            prompt (see ``build_compact_prompt``). Blank means the base prompt.
        system_prompt: System prompt of the summary call.
        tools: Tool schemas of the summary call (None or [] = no tools).

    Returns:
        Extracted summary text (stripped of surrounding whitespace).
    """
    prompt = build_compact_prompt(extra_instructions)
    compact_messages = list(messages) + [{"role": "user", "content": prompt}]

    config = provider.config_type(max_tokens=MAX_OUTPUT_TOKENS_FOR_SUMMARY)

    response = await provider.complete(
        messages=compact_messages,
        system_prompt=system_prompt,
        tools=tools or [],
        config=config,
    )
    if tools and (_has_tool_use(response) or _extract_summary(_response_text(response)) is None):
        # A reused prefix exposes the agent's tools: a tool call (or a reply with
        # no summary) is retried once without them — the only call that pays the
        # full price for the history. A tool_use never enters state either way.
        response = await provider.complete(
            messages=compact_messages,
            system_prompt=system_prompt,
            tools=[],
            config=config,
        )

    raw_text = _response_text(response)
    summary = _extract_summary(raw_text)
    if summary is not None:
        return summary

    # No usable <summary> pair — return the full response as a best-effort fallback.
    return raw_text.strip()


def _response_text(response: AssistantResponse) -> str:
    """Concatenate the response's text blocks."""
    return "".join(block.text for block in response.content if getattr(block, "text", None))


def _has_tool_use(response: AssistantResponse) -> bool:
    return any(isinstance(block, ToolUseBlock) for block in response.content)
```

- [ ] **Step 4: Engine** — in `friday_agent/core/engine.py`:
  1. Imports: add `from friday_agent.api.prompts import assemble_system_prompt`, and change the compact import to `from friday_agent.context.compact import SUMMARIZER_SYSTEM_PROMPT, compact_conversation, create_compact_summary_message`.
  2. Add two private methods after `__init__`:

     ```python
         def _effective_system_prompt(self) -> str:
             """Static system prefix: memory instructions (when mounted) -> domain prompt.

             Must stay byte-stable within a session — it heads the cached prefix, and
             compact(reuse_prefix=True) reproduces it to read the conversation cache.
             """
             memory_section = MEMORY_INSTRUCTIONS if self._memory is not None else ""
             return "\n\n".join(p for p in (memory_section, self._system_prompt) if p)

         def _tool_schemas(self) -> list[dict]:
             return [tool.get_tool_schema() for tool in self._tools]
     ```

  3. In `step()`, replace the three lines `memory_section = ...`, `parts = ...`, `effective_prompt = "\n\n".join(parts)` with `effective_prompt = self._effective_system_prompt()`. Delete the `tool_schemas = [tool.get_tool_schema() for tool in self._tools]` line and pass `tool_schemas=self._tool_schemas(),` to `run_one_turn`.
  4. Replace `compact()` with:

```python
    async def compact(self, state: LoopState, *, reuse_prefix: bool = False) -> LoopState:
        """Summarize the entire conversation into one summary message and return a smaller LoopState.

        Recovery entry point for context overflow (and for proactive compaction):
        when a turn cannot fit the model's context window, call compact(state) to
        replace all of state.messages with a single summary message, then retry.
        turn_count and todos are preserved.

        By default the summarizer runs under SUMMARIZER_SYSTEM_PROMPT with no tools —
        compact_instructions (constructor) is the injection point for domain
        requirements about what the summary must preserve. turn_sections are never
        rendered here.

        Args:
            state: The state to compact.
            reuse_prefix: Send the summary call with exactly the system prompt and
                tool schemas step() sends, so the provider can serve the
                conversation from its prompt cache instead of writing it again. A
                reply that calls a tool or lacks a usable <summary> is retried once
                with tools=[]. Best for proactive compaction: the agent's prefix adds
                tokens, so during overflow recovery the summary call itself can
                overflow; and with extended thinking enabled in this agent's config,
                the summary call (thinking off) cannot reuse the message cache.

        Raises:
            PendingToolUseError: the state still has unanswered tool_use blocks
                (its summary call would send them unpaired).
        """
        if pending := pending_tool_uses(state):
            raise PendingToolUseError([block.id or "" for block in pending])
        if reuse_prefix:
            system_prompt = str(assemble_system_prompt(self._effective_system_prompt()))
            tool_schemas = self._tool_schemas()
        else:
            system_prompt, tool_schemas = SUMMARIZER_SYSTEM_PROMPT, []
        summary_text = await compact_conversation(
            provider=self._provider,
            messages=normalize_for_api(state.messages),
            extra_instructions=self._compact_instructions,
            system_prompt=system_prompt,
            tools=tool_schemas,
        )
        summary_message = create_compact_summary_message(summary_text)
        return LoopState(
            messages=[summary_message],
            turn_count=state.turn_count,
            todos=state.todos,
        )
```

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/test_compact_reuse_prefix.py tests/test_compact.py tests/test_engine.py -v` → Expected: all PASS.
Run: `python -m pytest -q` → Expected: 305 passed.

- [ ] **Step 6: Update the docs**
  - `docs/architecture/04-context-compaction.md`
    - ③ flow: replace `         └─ compact_conversation(provider, messages, extra_instructions)` and its summarizer comment line with

      ```
               └─ compact_conversation(provider, messages, extra_instructions, system_prompt, tools)
                     #   default: summarizer system = SUMMARIZER_SYSTEM_PROMPT, tools=[]
                     #   reuse_prefix=True: system + tools byte-identical to step()'s
      ```

      and below `├─ provider.complete(tools=[], ...)` add `               │     # reuse_prefix: complete(tools=agent tools); a tool_use or no <summary> → retry once with tools=[]`.
    - After the "Domain Instruction Injection Slot (opt-in)" subsection, add:

      ```
      ### Same-Prefix Compaction (`reuse_prefix`, opt-in)

      By default the summary call's prefix (summarizer system prompt, no tools) differs from the agent's, so it cannot read the cached conversation — instead it writes the whole history to the cache at 1.25×, an entry nothing ever reads. `engine.compact(state, reuse_prefix=True)` sends the summary call with exactly the system prompt and tool schemas `step()` sends (both come from the same private helpers, so they cannot drift); the compaction prompt is the final user message. The history is then read at ~0.1×.

      - **Retry**: if the reply contains a `tool_use` or has no usable `<summary>`, the call is retried once with `tools=[]` (same system prompt) — only that retry pays the full price. A `tool_use` from the summarizer never enters state; only the summary text is used.
      - **Per call**: turn it on for proactive compaction. During overflow recovery, the agent's system and tool tokens can push the summary call itself over the window.
      - **Thinking**: thinking settings are part of the cached prefix. With extended thinking enabled in the agent config, the summary call (thinking off) cannot reuse the message cache.
      - **Default off**: `compact(state)` is unchanged.
      ```

    - ④: the `compact` signature becomes `async def compact(self, state: LoopState, *, reuse_prefix: bool = False) -> LoopState:` with the bullet `- `reuse_prefix=True`: same-prefix summary call (see above)`. The `compact_conversation` signature block gains `    system_prompt: str = SUMMARIZER_SYSTEM_PROMPT,` and `    tools: list[dict] | None = None,`, and its bullet becomes `- Default `tools=[]` (summarizer prompt). With `tools`, a tool_use or no `<summary>` triggers one `tools=[]` retry. Uses `config_type(max_tokens=20000)`.`
    - ⑥: the **`tools=[]` required** bullet becomes `- **No tool calls reach state**: on the default path, the `complete()` call inside `compact_conversation()` uses `tools=[]` — the enforcement mechanism (the prompt's no-tools line only helps third-party providers). With `reuse_prefix`, the first call carries the agent's tools; a `tool_use` reply is discarded and retried with `tools=[]`. Either way only the summary text is used, so `tool_use`↔`tool_result` pairing cannot break.` The **Separate summarizer system** bullet becomes `- **Separate summarizer system (default)**: by default the summary call runs under the dedicated `SUMMARIZER_SYSTEM_PROMPT`; `engine.compact()` passes the agent's own system prompt only with `reuse_prefix=True`.`
  - `docs/architecture/01-core-loop.md` — the ④ heading `### `await engine.compact(state) -> LoopState`` becomes `### `await engine.compact(state, *, reuse_prefix=False) -> LoopState``, with the line `With `reuse_prefix=True`, the summary call reuses `step()`'s exact system prompt and tools so the provider can serve the history from its prompt cache (see [04-context-compaction](04-context-compaction.md)).` added below its first sentence.
  - `CLAUDE.md` — replace `+ `engine.compact(state)` (caller-driven compact)` with `+ `engine.compact(state, *, reuse_prefix=False)` (caller-driven compact; `reuse_prefix=True` sends the summary call with `step()`'s exact system+tools so it reads the conversation cache)`.
  - `README.md` — in `### Injecting Domain Requirements into Compaction (opt-in)`, replace `The summarization call in `engine.compact()` runs with a dedicated summarizer system prompt` with `By default, the summarization call in `engine.compact()` runs with a dedicated summarizer system prompt`, and at the end of that section add: `Pass `reuse_prefix=True` (`await engine.compact(state, reuse_prefix=True)`) to send the summary call with the agent's own system prompt and tools — the provider can then serve the conversation from its prompt cache instead of writing it again. Use it for proactive compaction; leave it off when recovering from `ContextOverflowError`, since the extra prefix tokens can overflow the summary call itself.`

- [ ] **Step 7: Commit**

```bash
git rev-parse --abbrev-ref HEAD
git add friday_agent/context/compact.py friday_agent/core/engine.py tests/test_compact_reuse_prefix.py docs/architecture/04-context-compaction.md docs/architecture/01-core-loop.md CLAUDE.md README.md
git commit -F - -- friday_agent/context/compact.py friday_agent/core/engine.py tests/test_compact_reuse_prefix.py docs/architecture/04-context-compaction.md docs/architecture/01-core-loop.md CLAUDE.md README.md <<'EOF'
feat(compact): reuse_prefix — summarize under the agent's own prefix

compact(state, reuse_prefix=True) sends the summary call with exactly the
system prompt and tool schemas step() sends, so the provider can read the
cached conversation (~0.1x) instead of writing it again (1.25x). A reply
that calls a tool or lacks a usable <summary> is retried once with
tools=[]. Off by default.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JRQLSczEWcmgFp8vJsUs9q
EOF
git show --stat HEAD
```

---

### Task 10: Real-API verification scripts

**Files:**
- Create: `scripts/verify/verify_image.py`
- Create: `scripts/verify/verify_deferred.py`
- Modify: `scripts/verify/verify_cache.py`

**Interfaces:**
- Consumes: `ToolResult.image` (T5); `Suspended`, `resume`, `pending_tool_uses`, `Tool.is_deferred` (T8); `turn_sections` (T4); `compact(reuse_prefix=)` (T9); `Terminal.state` (T2).

- [ ] **Step 1: Create `scripts/verify/verify_image.py`:**

```python
"""verify_image.py — Real-API check for image-bearing tool results.

Confirms what fake-provider tests cannot: that the backend accepts a
tool_result whose content is a [text, image] block array.

  * claude-* models: the model actually sees the image (names its color).
  * gpt-* models: the adapter flattens the image to a text marker; the API
    accepts the request and no base64 reaches the payload.

Usage:
    LLM_MODEL=<model-id> python scripts/verify/verify_image.py

Cost guardrail: max_tokens=256; caller-side turn cap=4; one 32x32 PNG.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
import sys
import zlib
from pathlib import Path

from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # scripts/ -> import _env
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root -> friday_agent (when not pip-installed)
from _env import create_config, create_provider, resolve_api_key
from friday_agent.api.openai_provider import OpenAIProvider
from friday_agent.core.engine import FridayAgent
from friday_agent.core.state import LoopState, Terminal
from friday_agent.messages.types import Message, create_user_message
from friday_agent.tools.base import Tool, ToolResult


def _solid_png(rgb: tuple[int, int, int] = (255, 0, 0), size: int = 32) -> str:
    """Base64 PNG of one solid color, built with the stdlib (no imaging dependency)."""
    row = b"\x00" + bytes(rgb) * size  # filter type 0 + RGB pixels

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(row * size))
        + chunk(b"IEND", b"")
    )
    return base64.b64encode(png).decode()


IMAGE = {"media_type": "image/png", "data": _solid_png()}


class _NoInput(BaseModel):
    pass


class Snapshot(Tool):
    """Takes a snapshot of the current screen and returns it as an image."""

    name = "snapshot"

    def input_schema(self) -> type[BaseModel]:
        return _NoInput

    async def call(self, args: dict) -> ToolResult:
        return ToolResult(data="Snapshot captured.", image=IMAGE)


class _Recording:
    """Duck-typed LLMProvider wrapper that captures each complete() call's messages."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.config_type = inner.config_type
        self.received_messages: list[list[dict]] = []

    async def complete(self, messages, system_prompt, tools, config):
        self.received_messages.append(messages)
        return await self._inner.complete(
            messages=messages, system_prompt=system_prompt, tools=tools, config=config
        )


def _has_image_result(api_messages: list[dict]) -> bool:
    return any(
        isinstance(block.get("content"), list)
        and any(part.get("type") == "image" for part in block["content"])
        for msg in api_messages
        if msg.get("role") == "user"
        for block in msg["content"]
        if block.get("type") == "tool_result"
    )


async def main() -> int:
    print("=" * 60)
    print("Verification: Image-bearing tool results (Real API)")
    print("=" * 60)

    model = os.environ.get("LLM_MODEL", "")
    if not model:
        sys.exit("Set the LLM_MODEL environment variable to a real model ID.")

    provider = _Recording(create_provider(model, api_key=resolve_api_key(model)))
    engine = FridayAgent(
        provider=provider,
        tools=[Snapshot()],
        system_prompt="Call snapshot exactly once, then answer in one short sentence.",
        config=create_config(model, max_tokens=256),
    )
    state = LoopState(messages=[create_user_message(
        "Take a snapshot and tell me the single dominant color of the image."
    )])

    collected: list[Message] = []
    terminal: Terminal | None = None
    for _ in range(4):  # caller-side turn cap
        outcome = None
        async for item in engine.step(state):
            if isinstance(item, (LoopState, Terminal)):
                outcome = item
            else:
                collected.append(item)
        if isinstance(outcome, Terminal):
            terminal = outcome
            break
        state = outcome

    final_text = " ".join(
        block.text or ""
        for msg in collected if msg.type == "assistant"
        for block in msg.content if block.type == "text"
    )
    print(f"\nfinal answer : {final_text!r}")
    print(f"terminal     : {terminal.reason if terminal else '(turn cap reached)'}")
    if terminal and terminal.error:
        print(f"error        : {terminal.error}")

    checks = {
        "snapshot result sent as a [text, image] tool_result":
            any(_has_image_result(msgs) for msgs in provider.received_messages),
        "API accepted the request (reason == 'completed')":
            terminal is not None and terminal.reason == "completed",
    }
    if model.startswith("claude-"):
        checks["model saw the image (answer mentions red)"] = "red" in final_text.lower()
    else:
        payload = json.dumps([OpenAIProvider._to_openai_messages(msgs, "") for msgs in provider.received_messages])
        checks["OpenAI payload carries the marker, not the base64"] = (
            "[image omitted" in payload and IMAGE["data"] not in payload
        )

    print("\n--- Checklist ---")
    all_pass = True
    for label, ok in checks.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        all_pass = all_pass and ok
    print("\n" + ("=" * 20 + " PASS " + "=" * 20 if all_pass else "=" * 20 + " FAIL " + "=" * 20))
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```

- [ ] **Step 2: Create `scripts/verify/verify_deferred.py`:**

```python
"""verify_deferred.py — Real-API check for deferred tools (Suspended / resume).

Confirms what fake-provider tests cannot: that the backend accepts the
histories the deferred flow produces.

  A. approve: the model calls the deferred `approval` tool (next to ExampleTool)
     → Suspended → the state crosses a JSON boundary → resume() with the
     approval → step() completes.
  B. cancel: same start → resume() with an is_error result → a new user message
     → step() completes.

Usage:
    LLM_MODEL=<model-id> python scripts/verify/verify_deferred.py

Cost guardrail: max_tokens=512; caller-side turn cap=4 per phase.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # scripts/ -> import _env
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root -> friday_agent (when not pip-installed)
from _env import create_config, create_provider, resolve_api_key
from friday_agent.core.engine import FridayAgent
from friday_agent.core.loop import pending_tool_uses, resume
from friday_agent.core.state import LoopState, Suspended, Terminal
from friday_agent.messages.types import create_user_message
from friday_agent.tools.base import Tool, ToolResult
from friday_agent.tools.builtin.example_tool import ExampleTool

_CAP = 4

SYSTEM_PROMPT = (
    "You publish documents for the user. Before publishing, ALWAYS call the approval "
    "tool with action='publish'. In the same response, also call ExampleTool once with "
    "payload='draft'. After the results arrive, reply in one short sentence."
)


class ApprovalInput(BaseModel):
    action: str = Field(description="The action that needs a human's approval")


class Approval(Tool):
    """Requests a human's approval for an action. The decision arrives later."""

    name = "approval"

    def input_schema(self) -> type[BaseModel]:
        return ApprovalInput

    def is_deferred(self, input: dict) -> bool:
        return True

    async def call(self, args: dict) -> ToolResult:
        return ToolResult(data="approval runs outside step()", is_error=True)


async def _run(engine: FridayAgent, state: LoopState) -> LoopState | Suspended | Terminal:
    """Step until the loop stops: a Suspended, a Terminal, or the turn cap."""
    outcome: LoopState | Suspended | Terminal = state
    for _ in range(_CAP):
        async for item in engine.step(outcome):
            if isinstance(item, (LoopState, Suspended, Terminal)):
                outcome = item
        if not isinstance(outcome, LoopState):
            break
    return outcome


def _describe(outcome) -> str:
    if isinstance(outcome, Suspended):
        return f"Suspended(pending={[b.name for b in outcome.pending]})"
    if isinstance(outcome, Terminal):
        return f"Terminal(reason={outcome.reason}, error={outcome.error!r})"
    return type(outcome).__name__


async def _phase(engine: FridayAgent, *, cancel: bool) -> dict[str, bool]:
    first = await _run(engine, LoopState(messages=[create_user_message("Publish the quarterly report.")]))
    print(f"  first : {_describe(first)}")
    if not isinstance(first, Suspended):
        return {"turn suspended on the deferred approval": False}

    state = LoopState.from_dict(json.loads(json.dumps(first.state.to_dict())))  # a process boundary
    pending = pending_tool_uses(state)
    if cancel:
        state = resume(state, {b.id: ToolResult(data="Cancelled by the user.", is_error=True) for b in pending})
        state.messages.append(create_user_message("Never mind, don't publish. Just reply OK."))
    else:
        state = resume(state, {b.id: ToolResult(data="Approved by the editor.") for b in pending})

    final = await _run(engine, state)
    print(f"  final : {_describe(final)}")
    return {
        "turn suspended on the deferred approval": True,
        "pending recomputed after the JSON round trip": [b.id for b in pending] == [b.id for b in first.pending],
        "API accepted the resumed history (reason == 'completed')":
            isinstance(final, Terminal) and final.reason == "completed",
    }


async def main() -> int:
    print("=" * 60)
    print("Verification: Deferred tools — Suspended / resume (Real API)")
    print("=" * 60)

    model = os.environ.get("LLM_MODEL", "")
    if not model:
        sys.exit("Set the LLM_MODEL environment variable to a real model ID.")

    engine = FridayAgent(
        provider=create_provider(model, api_key=resolve_api_key(model)),
        tools=[Approval(), ExampleTool()],
        system_prompt=SYSTEM_PROMPT,
        config=create_config(model, max_tokens=512),
    )

    checks: dict[str, bool] = {}
    for name, cancel in (("A. approve", False), ("B. cancel", True)):
        print(f"\n{name}")
        for label, ok in (await _phase(engine, cancel=cancel)).items():
            checks[f"{name}: {label}"] = ok

    print("\n--- Checklist ---")
    all_pass = True
    for label, ok in checks.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        all_pass = all_pass and ok
    print("\n" + ("=" * 20 + " PASS " + "=" * 20 if all_pass else "=" * 20 + " FAIL " + "=" * 20))
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```

- [ ] **Step 3: Extend `scripts/verify/verify_cache.py`:**
  1. Docstring: after the turn-2 bullet, add

     ```
       * Both turns carry a per-turn section (turn_sections) that changes every
         call — it rides messages[-1] as a <system-reminder>, so turn 2 must still
         read the cache.
       * Compaction with reuse_prefix=True (the call after turn 2):
         cache_read_input_tokens > 0 — the summary call reads the history the agent
         turns cached instead of writing it again. A default compaction runs once
         more for contrast (printed, not asserted).
     ```

     and change the cost line to `Cost guardrail: max_tokens=256 for turns (summary calls use the compaction default); four or five completion calls over a ~12K-token prefix.`
  2. Imports: add `import json`.
  3. `_RecordingProvider`: add `self.messages: list[list[dict]] = []` in `__init__`, and `self.messages.append(messages)` at the top of `complete`.
  4. Below `_RUN_NONCE`, add:

     ```python
     _TICKS = {"n": 0}


     async def _ticker(state: LoopState) -> str:
         """A per-turn section that differs on every call (it must not break the cache)."""
         _TICKS["n"] += 1
         return f"Per-turn note {_TICKS['n']}"
     ```

  5. `FridayAgent(...)`: add `turn_sections=[_ticker],`.
  6. After `_diag_turn(2, outcome2, collected2)`, add:

     ```python
         # Compaction over the same history: first with the agent's own prefix (must
         # read the cache the turns wrote), then the default path for contrast.
         state3 = outcome2.state if isinstance(outcome2, Terminal) else outcome2
         reuse_at = len(recorder.calls)
         await engine.compact(state3, reuse_prefix=True)
         default_at = len(recorder.calls)
         await engine.compact(state3)
     ```

  7. In `checks`, add:

     ```python
             "turn sections rode both turns (per-turn note 1 and 2 sent)":
                 "Per-turn note 1" in json.dumps(recorder.messages[0])
                 and "Per-turn note 2" in json.dumps(recorder.messages[1]),
             "compaction with reuse_prefix read cache (cache_read > 0)":
                 recorder.calls[reuse_at].cache_read_input_tokens > 0,
     ```

  8. Before the PASS/FAIL banner, add:

     ```python
         print(f"  compaction reuse_prefix : {_fmt(recorder.calls[reuse_at])}")
         print(f"  compaction default      : {_fmt(recorder.calls[default_at])}  (contrast — not asserted)")
     ```

- [ ] **Step 4: Smoke-check the scripts without spending tokens**

Run: `python -c "import ast,sys; [ast.parse(open(p).read(), p) for p in sys.argv[1:]]" scripts/verify/verify_image.py scripts/verify/verify_deferred.py scripts/verify/verify_cache.py && python -m pytest -q`
Expected: no `SyntaxError`; 305 passed.

- [ ] **Step 5: Commit**

```bash
git rev-parse --abbrev-ref HEAD
git add scripts/verify/verify_image.py scripts/verify/verify_deferred.py scripts/verify/verify_cache.py
git commit -F - -- scripts/verify/verify_image.py scripts/verify/verify_deferred.py scripts/verify/verify_cache.py <<'EOF'
test(verify): real-API checks for images, deferred tools, sections and compaction cache

verify_image.py: Anthropic sees an image tool_result; OpenAI gets the
marker. verify_deferred.py: approve and cancel flows across a JSON
boundary. verify_cache.py: turn sections keep the cache hitting, and
reuse_prefix compaction reads it.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JRQLSczEWcmgFp8vJsUs9q
EOF
git show --stat HEAD
```

---

### Task 11: Run the real-API checks (user go-ahead required — token cost)

**Files:** none, unless a run exposes a defect. A fix goes through TDD (failing unit test first), gets its own commit, and the affected script is rerun.

- [ ] **Step 1: Ask the user for the go-ahead.** List the runs below and say they use the keys in `.env` and cost tokens (small models, capped `max_tokens`). Wait for an explicit yes.

- [ ] **Step 2: Claude runs** (expect `PASS` from each)

```bash
LLM_MODEL=claude-haiku-4-5 python scripts/verify/verify_image.py
LLM_MODEL=claude-haiku-4-5 python scripts/verify/verify_deferred.py
LLM_MODEL=claude-haiku-4-5 python scripts/verify/verify_cache.py
LLM_MODEL=claude-haiku-4-5 python scripts/verify/verify_todo.py
LLM_MODEL=claude-haiku-4-5 python scripts/verify/verify_p2.py
LLM_MODEL=claude-haiku-4-5 python scripts/verify/verify_p4.py
```

- [ ] **Step 3: OpenAI runs** (expect `PASS` from each). The adapter sends `max_tokens`, so use a chat model that accepts it — `gpt-4.1-mini`, or `gpt-4o-mini` if that one is unavailable.

```bash
LLM_MODEL=gpt-4.1-mini python scripts/verify/verify_todo.py      # the Task 3 ordering fix: reminder after tool results
LLM_MODEL=gpt-4.1-mini python scripts/verify/verify_image.py
LLM_MODEL=gpt-4.1-mini python scripts/verify/verify_deferred.py
LLM_MODEL=gpt-4.1-mini python scripts/verify/verify_p2.py
```

- [ ] **Step 4: Report** each script's checklist to the user, verbatim for any FAIL. A FAIL caused by model behavior (for example, the model skipped the tool) is rerun once and reported as such. A FAIL caused by a request the API rejects is a defect — fix it under TDD and rerun.

---

### Task 12: Final sweep and cleanup

**Files:**
- Modify: any doc whose statements or `file:line` references went stale
- Delete: `docs/superpowers/specs/2026-10-03-loop-contracts-design.md`, `docs/superpowers/plans/2026-10-03-loop-contracts.md`

- [ ] **Step 1: Grep for stale contract statements**

Run:

```bash
grep -rn -E "LoopState \| Terminal[^|]|LoopState or Terminal|\(LoopState, Terminal\)|yield_missing_tool_result_blocks|backfill|no engine-level dynamic|injection surface for dynamic|content: str \| None" docs/architecture CLAUDE.md README.md friday_agent
```

Expected: no hits, except `(LoopState, Terminal)` in README example code that never meets a deferred tool (Quick Start, and the first Distributed Resume example), which stays correct as written. Fix every other hit so it matches the final API.

- [ ] **Step 2: Check `file:line` references in the architecture docs**

Run:

```bash
python - <<'EOF'
import pathlib, re
for doc in sorted(pathlib.Path("docs/architecture").glob("*.md")):
    for m in re.finditer(r"((?:friday_agent/)?(?:core|api|tools|context|messages|memory)/[a-z_]+\.py):(\d+)", doc.read_text()):
        path = pathlib.Path(m.group(1) if m.group(1).startswith("friday_agent/") else "friday_agent/" + m.group(1))
        lines = path.read_text().splitlines() if path.exists() else []
        line = int(m.group(2))
        code = lines[line - 1].strip() if 0 < line <= len(lines) else "<out of range>"
        print(f"{doc.name:28} {m.group(0):45} -> {code[:70]}")
EOF
```

Expected: each reference points at the line it names (the definition or statement the sentence talks about). Fix the drifted ones by pointing at the new line, or by naming the function instead of a line number.

- [ ] **Step 3: Full suite** — Run: `python -m pytest -q` → Expected: 305 passed.

- [ ] **Step 4: Commit the doc fixes (only if Steps 1–2 changed anything)** — set `FILES` to exactly the paths Steps 1–2 edited (e.g. `FILES="docs/architecture/01-core-loop.md docs/architecture/02-tool-orchestration.md"`):

```bash
git rev-parse --abbrev-ref HEAD
git add $FILES
git commit -F - -- $FILES <<'EOF'
docs: refresh references after the loop contract changes

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JRQLSczEWcmgFp8vJsUs9q
EOF
git show --stat HEAD
```

- [ ] **Step 5: Remove the working spec, the plan, and the progress note** — first delete the whole `## In-Progress Work — `refactor/loop-contracts` (remove when done)` section from `CLAUDE.md` (heading through its last bullet), then:

```bash
git rev-parse --abbrev-ref HEAD
git rm docs/superpowers/specs/2026-10-03-loop-contracts-design.md docs/superpowers/plans/2026-10-03-loop-contracts.md
git add CLAUDE.md
git commit -F - -- docs/superpowers/specs/2026-10-03-loop-contracts-design.md docs/superpowers/plans/2026-10-03-loop-contracts.md CLAUDE.md <<'EOF'
chore: drop the working spec, plan and progress note

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JRQLSczEWcmgFp8vJsUs9q
EOF
git show --stat HEAD
git log --oneline main..HEAD
```

Expected: `git log` lists the spec commit, Tasks 1–10, any Task 11 fixes and Task 12 commits, and this cleanup — all on `refactor/loop-contracts`.
