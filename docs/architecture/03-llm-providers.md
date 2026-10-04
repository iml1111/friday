# 03 — LLM Provider Boundary

Defines the boundary for swapping LLM backends. Every completion call passes through this layer, and vendor-specific responses are normalized into `AssistantResponse`.

---

## ① Purpose

The `api/` package consolidates three responsibilities into a single swap boundary.

1. **Abstract interface** — `LLMProvider(ABC)` + common types (`AssistantResponse`, `StopReason`, `ContentBlock`, `TokenUsage`)
2. **Error hierarchy** — normalizes vendor exceptions into 5 classes so the loop does not depend on vendor SDKs.
3. **Vendor adapters** — Anthropic / OpenAI each implement the same interface.

`core/loop.py` and `context/compact.py` call only `LLMProvider.complete()`. Swapping the adapter requires no change to the loop code.

---

## ② Owned Files

| Path | Responsibility | Key Symbols |
|---|---|---|
| `api/provider.py` | Abstract boundary · common types · 5-class errors | `LLMProvider`, `LLMConfig`, `AssistantResponse`, `StopReason`, `ContentBlock`(union) |
| `api/configs.py` | Per-vendor call config | `AnthropicConfig`, `OpenAIConfig` |
| `api/anthropic_provider.py` | Anthropic adapter | `AnthropicProvider` |
| `api/openai_provider.py` | OpenAI adapter | `OpenAIProvider` |
| `api/prompts.py` | System prompt assembly | `SystemPrompt`, `assemble_system_prompt()` |

---

## ③ Core Behavior / Types

### LLMProvider

`api/provider.py:107` — `LLMProvider(ABC, Generic[ConfigT])` enforces a single abstract method, `complete()`.

| Member | Signature | Description |
|---|---|---|
| `complete()` | `async (messages, system_prompt, tools, config) -> AssistantResponse` | Single completion call. The adapter calls the vendor SDK and normalizes the response. |

The class attribute `config_type: type[ConfigT]` must be set by the adapter. `FridayAgent` uses it to validate config/provider mismatches and to build the default config (`provider.config_type()`).

### Common Types

`api/provider.py:34` — `ContentBlock = Union[TextBlock, ToolUseBlock, ThinkingBlock]`

- `TextBlock(type, text)` — text response block
- `ToolUseBlock(type, id, name, input)` — tool call block. `input` is always a `dict`
- `ThinkingBlock(type, thinking, signature)` — extended thinking block (Anthropic only)

`api/provider.py:55` — `AssistantResponse(content, stop_reason, usage, id, model)`

`api/provider.py:37` — `StopReason` enum:

| Value | Meaning |
|---|---|
| `END_TURN` | Normal termination |
| `TOOL_USE` | Tool call requested |
| `MAX_TOKENS` | Output token limit reached |
| `CONTEXT_WINDOW_EXCEEDED` | Context window exceeded (defensive mapping; actual overflow is delivered as an exception) |

`api/provider.py:46` — `TokenUsage(input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens)`. The cache fields are 0 for backends that do not support caching. With prompt caching always-on, these fields are populated — measured non-zero on Anthropic, and via automatic caching on OpenAI (see ④ Per-Adapter Differences).

### LLMConfig (Protocol)

`api/provider.py:90` — `@runtime_checkable class LLMConfig(Protocol)`. Declares only the common levers `max_tokens: int` and `temperature: float | None`. Vendor configs are fully separated with no shared base (see Design Rationale ⑦).

### Vendor Config

`api/configs.py` — pure dataclasses that do not import vendor SDKs. Config construction, inspection, and routing are possible without loading an SDK.

```
AnthropicConfig(max_tokens=16384, temperature=None, thinking_enabled=False, thinking_budget=None)
OpenAIConfig(max_tokens=16384, temperature=None)
```

### 5-Class Error Hierarchy

`api/provider.py:64–86` — every vendor exception is mapped to one of five classes before it reaches the loop.

```
LLMError (base)
├── RateLimitError       — 429 too many requests
├── ContextOverflowError — context window exceeded → triggers caller compact
├── AuthError            — auth failure, e.g. expired API key
└── TransientError       — transient errors, e.g. network timeout, 5xx
```

`ContextOverflowError` is raised by the adapter after classifying a 400 response; the caller (the side using `FridayAgent.step()`) retries after `engine.compact(state)`.

### Provider Construction

The library provides no provider construction factory — adapters (`AnthropicProvider`/`OpenAIProvider`) are instantiated directly (`api_key` required, externally injected; the adapter raises `ValueError` if empty). Callers that need model-prefix (`claude-`/`gpt-`) based routing handle it themselves in the boundary layer (e.g. `create_provider(model, api_key=...)` in `scripts/_env.py` — lazy-imports the adapter module only in the matching branch).

### System Prompt Assembly (prompts.py)

`api/prompts.py:13` — `SystemPrompt(text)` implements `__str__` and is passed directly to `LLMProvider.complete(system_prompt=str(sp))`.

- `assemble_system_prompt(system_prompt)` — injects `GENERAL_AGENT_GUIDANCE`·`TODO_GUIDANCE` **before** the base prompt, then wraps it in `SystemPrompt` (generic→specific: domain rules get the recency advantage). If base is empty, returns only the guidance.

---

## ④ Per-Adapter Differences

The core of the vendor boundary — describes where the two adapters behave differently.

| Item | Anthropic (`api/anthropic_provider.py`) | OpenAI (`api/openai_provider.py`) |
|---|---|---|
| **Tool input parsing** | SDK pre-parses `tool_use.input` into a dict. Do not re-parse (`_normalize_block:234`) | `tool_calls[].function.arguments` is a JSON string → `json.loads` (`_parse_arguments:308`) |
| **Message format** | Content blocks passed as-is | `tool_use`→`tool_calls`, `tool_result`→`{"role":"tool"}` (emitted **before** the same turn's text — reminders ride the tool_result turn, and tool messages must directly follow `tool_calls`), system→leading message, thinking dropped (`_to_openai_messages`) |
| **tool_result images** | `[text, image]` block array sent as-is | Flattened by `_flatten_tool_result_content`: text blocks joined, each image replaced by `[image omitted: not supported by the OpenAI adapter]` (Chat Completions tool messages are text-only) |
| **Empty tools** | Field omitted entirely (`_build_params:144`) | Same — field omitted entirely (`_build_params:168`) |
| **thinking** | `temperature` not sent when `thinking_enabled=True` (`_build_params:148`) | thinking not supported |
| **Overflow detection** | 400/413 + message signal check (`_is_context_overflow:318`) | 400/413 + `body.error.code=="context_length_exceeded"` or message check (`_is_context_overflow:382`) |
| **Unmapped stop_reason** | Falls back to `END_TURN` (`_map_stop_reason:260`) | Falls back to `END_TURN` (`_map_stop_reason:326`) |
| **Prompt caching** | always-on. `_apply_cache_control` places `cache_control:{ephemeral}` on the last system block (=tools+system) + the last **persistent** block of the last/second-to-last message (skipping trailing `<system-reminder>` reminders) | Automatic (no request-side opt-in). `_extract_usage` reads `prompt_tokens_details.cached_tokens` |

---

## ⑤ Dependencies

### External Dependencies

- **Standard library** — `abc`, `dataclasses`, `enum`, `typing`, `json`
- **`anthropic` SDK** — imported only in `AnthropicProvider` (lazy)
- **`openai` SDK** — imported only in `OpenAIProvider` (lazy)

`api/configs.py` imports no vendor SDK — config construction and inspection work without an SDK.

### Internal Dependencies

```
core/loop.py         → provider.complete()  call
context/compact.py   → provider.complete()  call (when generating the summary)
core/engine.py       → provider.config_type validation
```

Dependency direction within the `api/` package: `anthropic_provider` / `openai_provider` → `provider` ← `configs`.

---

## ⑥ Maintenance Notes

The following invariants must be preserved when modifying adapters. See [06-invariants](06-invariants.md) for the full list.

1. **No temperature with thinking** — when `AnthropicConfig.thinking_enabled=True`, the `temperature` parameter must not be sent to the API (`anthropic_provider.py:148–154`).
2. **Omit empty tools** — sending `tools=[]` to the API causes request rejection on some models. Both adapters omit the `tools` field when the list is empty.
3. **`ContextOverflowError` propagation** — when the adapter classifies a 400 and raises, `core/loop.py` does not catch it and propagates it to the caller. The caller retries after `engine.compact(state)`. See [04-context-compaction](04-context-compaction.md) for the context compaction flow.
4. **OpenAI argument parsing** — `_parse_arguments` returns an empty dict `{}` on JSON parse failure. When modifying the adapter, take care not to leak parse exceptions out of the loop.
5. **Prompt caching always-on** — on every call, `_apply_cache_control` places `cache_control:{ephemeral}` on the last system block and on the last **persistent** block of the last/second-to-last message (system+tools prefix + conversation history). Trailing turn-local reminders (`<system-reminder>` blocks — detected by prefix match on the shared constant `SYSTEM_REMINDER_PREFIX` in `messages/types.py`; producers wrap them with `wrap_system_reminder()` from the same module) are skipped during breakpoint selection — they are non-persistent blocks that vanish from that position on the next call, so a cache entry from a breakpoint placed there never hits, and the previous turn's new tool_result gets cache-written one more time on the next call (~35% of cache writes wasted in a measured session). Skipped reminders are billed as regular input (1×) after the breakpoint. Messages consisting entirely of reminders get no mark (covered by the `messages[-2]` anchor). The prefix must be byte-for-byte stable to hit; below the per-model minimum cache size (Opus/Haiku 4.x=4096, Sonnet 4.6=2048 tokens) the markers are harmless and `cache_creation=0`. There is a 4-breakpoint limit and a 20-block lookback constraint. `messages[-2]` is used as the stable anchor because per-turn reminders (todo · memory index) are attached only to `messages[-1]`. There is no config knob (aligned with OpenAI's unavoidable automatic caching and the always-on built-in policy).

---

## ⑦ Design Rationale (Why)

### Fully Separated Vendor Configs

`AnthropicConfig` and `OpenAIConfig` are fully separate dataclasses with no shared base class. The two configs have different parameter sets (`thinking_enabled`, `thinking_budget` vs none), and a common base would expose one vendor's fields on the other vendor's config. Keeping `LLMConfig(Protocol)` as a runtime-checkable structural marker that declares only the common levers (`max_tokens`, `temperature`) achieves full separation while preserving type safety.

### Direct Adapter Module Import

Importing only the needed adapter module directly (e.g. `from friday_agent.api.anthropic_provider import AnthropicProvider`) means a Claude-only setup works even without the `openai` package installed — achieving both dependency isolation and lower import cost. Each vendor SDK (`anthropic`/`openai`) is also imported only inside its adapter module. (When the caller uses prefix routing, `create_provider` in `scripts/_env.py` likewise imports the adapter only in the matching branch, preserving the same isolation.)

### 5-Class Error Hierarchy

If the loop (`core/loop.py`) caught vendor SDK exception classes directly, swapping adapters would also require changing the loop code. When the adapter converts every exception into one of the 5 classes and raises it, the loop can handle them identically regardless of vendor.
