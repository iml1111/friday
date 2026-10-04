# 06 — Par-Critical Invariants

## What the Invariant Is

"Par-critical integrity" refers to the rule that **`tool_use` blocks and `tool_result` blocks must correspond 1:1**. The LLM API rejects the request outright unless every `tool_use` block that appeared in the previous assistant message has a matching `tool_result` in the next user message. This constraint must not break on any execution path, including error recovery and parallel execution. The single, explicit exception is a `Suspended` state: its last assistant message may hold deferred `tool_use` blocks whose results arrive later through `resume()` — and no request is ever sent while one is pending. The table below lists the core invariants that guarantee loop integrity across the system; each entry links to the concrete guarantee point in the source code.

---

## Invariant List

| Invariant | Why (if broken) | Guaranteed at |
|---|---|---|
| Every `tool_use` has a matching `tool_result` — **except** the trailing assistant message of a `Suspended` state, whose deferred calls are pending | LLM API rejects the request | `core/loop.py` `run_one_turn()` — `run_tools()` emits exactly one `tool_result` per executed `tool_use` (errors included) and deferred calls end the turn as `Suspended(state, pending)`; `pending_tool_uses()` recomputes pending calls from history, and `step()`/`compact()` raise `PendingToolUseError` before any request while one remains (also when a user message was appended after it); `resume()` accepts only pending ids. The only error path (`LLMError` from the provider call) fires before an assistant message exists → [01-core-loop](01-core-loop.md) |
| Results keep the original `tool_use` block order — under parallel execution and when deferred results arrive later | Breaks pair matching and reproducibility | `tools/orchestrator.py` `run_tools()` — `asyncio.gather` returns results in argument order, so block order is preserved regardless of completion order; `core/loop.py` `resume()` inserts late results by `tool_use` order, ahead of any other trailing message → [02-tool-orchestration](02-tool-orchestration.md)·[01-core-loop](01-core-loop.md) |
| `tool_result` message `role="user"`, first message `user`, user/assistant alternation | API rejects on role-rule violation | `messages/types.py:91·117` — `create_tool_result_message()`·`create_user_message()` create with `role="user"`; `messages/normalize.py` — passes the role tag through as-is during API serialization → [05-messages](05-messages.md) |
| `step()` sends the entire `state.messages` to the API (window management = caller) | Arbitrary truncation loses context | `core/loop.py:219` — passes the whole `api_input_messages = with_turn_reminders(list(state.messages), …)` (only appends turn-local reminders, no truncation); overflow propagates to the caller as `ContextOverflowError` → `engine.compact()` retry → [01-core-loop](01-core-loop.md)·[04-context-compaction](04-context-compaction.md) |
| `temperature` not sent when thinking is enabled | Anthropic API rejects the two parameters together | `api/anthropic_provider.py:148-154` `_build_params()` — if `cfg.thinking_enabled`, sets only the `thinking` parameter; `temperature` is an `elif` branch (mutually exclusive) → [03-llm-providers](03-llm-providers.md) |
| Empty `tools=[]` omits the `tools` field entirely | Some models treat an empty array as an error | `api/anthropic_provider.py:145-146` `if tools: params["tools"] = tools`; `api/openai_provider.py:141-142` `if oa_tools: params["tools"] = oa_tools` — both adapters apply the same guard in `_build_params()` → [03-llm-providers](03-llm-providers.md) |
| thinking blocks are echoed verbatim | Omitting one breaks the API turn sequence (Anthropic requirement) | `messages/normalize.py:59-66` — the `block.type == "thinking"` branch inserts `{"type": "thinking", "thinking": block.text}` as-is; the comment states "omitting one breaks the API turn" → [05-messages](05-messages.md) |
| Cache prefix byte stability (always-on prompt caching) | Any change invalidates that tier's cache — **cost invariant** (harmless to integrity: a miss does not affect output) | `api/anthropic_provider.py` `_apply_cache_control()` — places `cache_control:{ephemeral}` on the last system block + the last **persistent** block of the last/second-to-last messages (skips trailing `<system-reminder>` reminders — a breakpoint on a non-persistent block cannot be reused); `messages[-2]` is the stable anchor (per-turn reminders go only on `messages[-1]`) → [03-llm-providers](03-llm-providers.md) |

---

## Cross-References

For the detailed implementation of each invariant, see the owning subsystem doc:

- **[01-core-loop](01-core-loop.md)** — `run_one_turn()`, caller-driven compaction flow
- **[02-tool-orchestration](02-tool-orchestration.md)** — `run_tools()` parallel execution · order preservation
- **[03-llm-providers](03-llm-providers.md)** — `_build_params()` thinking/temperature mutual exclusion, empty tools handling, `_apply_cache_control()` cache breakpoint placement
- **[04-context-compaction](04-context-compaction.md)** — `ContextOverflowError` propagation, `engine.compact()` retry contract
- **[05-messages](05-messages.md)** — role rules, thinking echo, `normalize_for_api()` serialization
