# 07. Data Model Catalog

A reference doc for seeing every data model in `friday_agent` at a glance. It holds only each model's **field summary · serialization status · definition location**, and links to the relevant subsystem doc for detailed behavior and conversion rules.

> This doc is a catalog. In-depth explanations (conversion flow, invariants, design rationale) are not duplicated here but delegated to [01](01-core-loop.md)·[02](02-tool-orchestration.md)·[03](03-llm-providers.md)·[05](05-messages.md).

---

## ① Serialization Boundary (most important)

Only models with `to_dict()` / `from_dict()` are **transport units for distributed resume**. Everything else is treated as a container-local runtime object and re-injected on resume.

| Model | Definition | serde | Transport role |
|---|---|---|---|
| `Message` | `messages/types.py` | ✅ | Conversation history unit |
| `ContentBlock` (internal flat) | `messages/types.py` | ✅ | Block inside a Message |
| `LoopState` | `core/state.py` | ✅ | Loop state (messages + turn_count) |
| `Checkpoint` | `core/state.py` | ✅ | Turn-boundary resume sentinel — the transport unit is `json.dumps(checkpoint.to_dict())` |
| Everything else | — | ❌ | Runtime-only (provider, config, responses, tool results, etc.) |

---

## ② Models by Layer

### 2.1 Conversation Messages — `messages/types.py` → details [05-messages](05-messages.md)

#### `Message` — the conversation unit the loop manages as `state.messages`

| Field | Type | Meaning |
|---|---|---|
| `uuid` | `str` | Auto-generated UUID |
| `type` | `str` | Internal classification: `user` \| `assistant` \| `system` |
| `role` | `str` | API wire role: `user` \| `assistant` |
| `content` | `list[ContentBlock]` | Block list |
| `is_api_error_message` | `bool` | Synthetic error message (used in recovery branches) |
| `is_compact_summary` | `bool` | Summary produced by compaction |
| `is_meta` | `bool` | System-internal only (excluded from API sends) |

`type` (semantics) and `role` (wire) are distinct — `tool_result` also goes out with `role="user"`.

#### `ContentBlock` (internal flat) — represents every block kind as a single dataclass

| Field | Applies to `type` | Meaning |
|---|---|---|
| `type` | All | `text` \| `tool_use` \| `tool_result` \| `thinking` |
| `text` | `text`·`thinking` | Text / thinking content |
| `id`·`name`·`input` | `tool_use` | Call ID · tool name · arguments (dict, parsed by the SDK) |
| `tool_use_id`·`content`·`is_error` | `tool_result` | Matching tool_use ID · result text · error flag |

> **⚠️ Name collision**: this `ContentBlock` (internal flat dataclass) and the `ContentBlock` in `api/provider.py` (2.3 below, a Union alias) **share only the name and are distinct types**. When importing both into the same scope, distinguish them with `as`.

**Construction helpers**: `create_user_message()`, `create_tool_result_message()` (wraps in `<tool_use_error>…</tool_use_error>` when `is_error=True`). See [05](05-messages.md) for details.

---

### 2.2 Loop State / Control — `core/state.py` → details [01-core-loop](01-core-loop.md)

#### `LoopState` — serializable loop state

| Field | Type | Meaning |
|---|---|---|
| `messages` | `list[Message]` | Full history |
| `turn_count` | `int` (default 1) | Turn counter |

Non-serializable runtime objects such as provider and config are intentionally excluded.

#### `Checkpoint` — the "continue" sentinel

| Field | Type | Meaning |
|---|---|---|
| `state` | `LoopState` | Next-turn state |

When the loop continues after a turn completes, `run_one_turn()` yields it (in contrast to Terminal).

#### `Terminal` — the "terminate" sentinel

| Field | Type | Meaning |
|---|---|---|
| `reason` | `str` | Termination reason |
| `error` | `Exception \| None` | Error object (on model_error) |

> **⚠️ reason drift**: **the reasons `run_one_turn()` actually emits are just two, `completed`·`model_error`** (per the branch table in [01-core-loop](01-core-loop.md)). The `state.py` docstring also lists `blocking_limit`·`image_error`·`hook_stopped`, but these are not emitted on the current execution path. Context overflow is not a Terminal; it is raised to the caller as `ContextOverflowError`.

---

### 2.3 LLM Response (wire) — `api/provider.py` → details [03-llm-providers](03-llm-providers.md)

The result of `provider.complete()` normalizing a vendor response. `loop._to_assistant_message()` converts it into the internal `Message`/`ContentBlock`.

#### `AssistantResponse`

| Field | Type | Meaning |
|---|---|---|
| `content` | `list[ContentBlock]` (union below) | Response block list |
| `stop_reason` | `StopReason` | Normalized stop signal |
| `usage` | `TokenUsage` | Token counters |
| `id` | `str` | Response ID (default `""`) |
| `model` | `str` | Model name (default `""`) |

#### Response block union — `ContentBlock = TextBlock | ToolUseBlock | ThinkingBlock`

| Block | Fields |
|---|---|
| `TextBlock` | `type="text"`, `text` |
| `ToolUseBlock` | `type="tool_use"`, `id`, `name`, `input`(dict) |
| `ThinkingBlock` | `type="thinking"`, `thinking`, `signature` (some backends only) |

#### `StopReason` (str Enum)

| Member | Value |
|---|---|
| `END_TURN` | `"end_turn"` |
| `TOOL_USE` | `"tool_use"` |
| `MAX_TOKENS` | `"max_tokens"` |
| `CONTEXT_WINDOW_EXCEEDED` | `"model_context_window_exceeded"` |

#### `TokenUsage`

`input_tokens` · `output_tokens` · `cache_creation_input_tokens` · `cache_read_input_tokens` (all `int`, 0 on backends without cache support).

#### Exception hierarchy (not models, but flow-control types)

`LLMError`(base) → `RateLimitError` · `ContextOverflowError` · `AuthError` · `TransientError`. **`ContextOverflowError` triggers caller-driven compact** ([04-context-compaction](04-context-compaction.md)).

---

### 2.4 Call Configuration — `api/provider.py` · `api/configs.py` → details [03-llm-providers](03-llm-providers.md)

No shared base class (fully separated per vendor; undefined fields raise `TypeError` at construction).

| Model | Kind | Fields |
|---|---|---|
| `LLMConfig` | Protocol (`@runtime_checkable`) | `max_tokens: int`, `temperature: float \| None` — structural marker for the common levers |
| `AnthropicConfig` | dataclass | `max_tokens=16384`, `temperature=None`, `thinking_enabled=False`, `thinking_budget=None` |
| `OpenAIConfig` | dataclass | `max_tokens=16384`, `temperature=None` |

Routed and validated via `provider.config_type`; when unspecified, `provider.config_type()` defaults are used.

---

### 2.5 Tools — `tools/base.py` · `tools/orchestrator.py` → details [02-tool-orchestration](02-tool-orchestration.md)

#### `ToolResult` — tool execution result

| Field | Type | Meaning |
|---|---|---|
| `data` | `Any` | Execution result (string/structured data) |
| `is_error` | `bool` | Error flag (default `False`) |

#### `Batch` (orchestrator-internal) — partitioning output

| Field | Type | Meaning |
|---|---|---|
| `is_concurrency_safe` | `bool` | If `True`, blocks in the batch run in parallel; if `False`, runs alone, sequentially |
| `blocks` | `list[ContentBlock]` | This batch's tool_use blocks |

---

### 2.6 Prompts — `api/prompts.py` → details [03-llm-providers](03-llm-providers.md)

#### `SystemPrompt`

| Field | Type | Meaning |
|---|---|---|
| `text` | `str` | Fully assembled prompt |

Implements `__str__`, so it is passed directly as `provider.complete(system_prompt=str(sp))`. Created by `assemble_system_prompt()`.

---

## ③ Conversion & Transport Flow (at a glance)

```
provider.complete()
   └─ AssistantResponse(content=[TextBlock|ToolUseBlock|ThinkingBlock], stop_reason, usage)
        │  _to_assistant_message()          ← wire union → internal flat conversion
        ▼
   Message(content=[ContentBlock(flat)])    ← accumulated in state.messages
        │  normalize_for_api()              ← internal → API wire dict (excludes is_meta · thinking verbatim)
        ▼
   list[dict]  → next provider.complete()

[turn-boundary transport]
   LoopState(messages=[Message], turn_count)
        └─ Checkpoint(state)
              └─ json.dumps(checkpoint.to_dict())   ← distributed resume unit
```

---

## ④ Interfaces (not data models — for reference)

The following are interfaces/ABCs rather than dataclasses, so they are outside this catalog's scope.

| Symbol | Location | Doc |
|---|---|---|
| `Tool` (ABC) | `tools/base.py` | [02](02-tool-orchestration.md) |
| `LLMProvider` (ABC) | `api/provider.py` | [03](03-llm-providers.md) |
