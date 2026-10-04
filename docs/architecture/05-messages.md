# 05. Messages

## ① Purpose

Handles conversion between the internal conversation representation (`Message` / `ContentBlock`) and the LLM API wire format.
Provides the message construction helpers shared by the loop · orchestrator · compactor, plus the filter · normalization functions applied just before API transmission.

---

## ② Owned Files

| Path | Responsibility | Key Symbols |
|---|---|---|
| `friday_agent/messages/types.py` | Internal Message · ContentBlock · construction helpers + turn-local reminder protocol | `Message`, `ContentBlock`, `create_user_message()`, `create_tool_result_message()`, `wrap_system_reminder()`, `SYSTEM_REMINDER_PREFIX` |
| `friday_agent/messages/normalize.py` | API format conversion | `normalize_for_api()` |

---

## ③ Core Behavior / Types

### `ContentBlock` — `messages/types.py:8`

A single flat dataclass that represents every block kind with one type.

| Field | Type | Applies To | Description |
|---|---|---|---|
| `type` | `str` | All | `"text"` \| `"tool_use"` \| `"tool_result"` \| `"thinking"` |
| `text` | `str \| None` | `text`, `thinking` | Text content / thinking content |
| `id` | `str \| None` | `tool_use` | Tool call ID |
| `name` | `str \| None` | `tool_use` | Tool name |
| `input` | `dict \| None` | `tool_use` | Tool arguments (the Anthropic SDK passes them already parsed into a dict) |
| `tool_use_id` | `str \| None` | `tool_result` | ID of the corresponding tool_use |
| `content` | `str \| list[dict] \| None` | `tool_result` | Tool result text — or a `[text, image]` block array when the tool returned an image |
| `is_error` | `bool` | `tool_result` | Whether execution errored (default `False`) |

> **⚠️ Name collision warning**: `ContentBlock` in `messages/types.py` (single flat dataclass, defined above) and `ContentBlock` in `api/provider.py:34` (`Union[TextBlock, ToolUseBlock, ThinkingBlock]` alias) **share only the name and are completely separate types**. The former is a dataclass for internal representation; the latter is a Union alias of Anthropic SDK types. Watch for the collision when importing both files in the same scope.

---

### `Message` — `messages/types.py:48`

The internal conversation unit the loop manages as the `state.messages` list.

| Field | Type | Description |
|---|---|---|
| `uuid` | `str` | Auto-generated UUID |
| `type` | `str` | Internal classification: `"user"` \| `"assistant"` \| `"system"` |
| `role` | `str` | API wire role: `"user"` \| `"assistant"` |
| `content` | `list[ContentBlock]` | Block list |
| `is_api_error_message` | `bool` | Synthetic error message (used by the loop for recovery branching) |
| `is_compact_summary` | `bool` | Summary message produced by compaction |
| `is_meta` | `bool` | System-internal-only message (excluded from API transmission) |

---

### Construction Helpers

#### `create_user_message()` — `messages/types.py:81`

```python
def create_user_message(
    content: str | list[ContentBlock],
    *,
    is_meta: bool = False,
    is_compact_summary: bool = False,
) -> Message:
```

- If `content` is a `str`, it is auto-wrapped into a single `ContentBlock(type="text", text=content)`.
- Fixed to `type="user"`, `role="user"`.

#### `create_tool_result_message()` — `messages/types.py:98`

```python
def create_tool_result_message(
    tool_use_id: str,
    result_text: str,
    is_error: bool = False,
    image: dict | None = None,
) -> Message:
```

- Fixed to `type="user"`, `role="user"` (complies with the API role alternation rule).
- If `is_error=True`, `content` is wrapped as `<tool_use_error>result_text</tool_use_error>`.
- The `is_error` value is passed through to `ContentBlock.is_error` as-is.
- If `image` is given (`{"media_type", "data"}`, base64), `content` becomes `[{"type": "text", "text": ...}, {"type": "image", "source": {"type": "base64", ...}}]`; otherwise the string, exactly as before.

---

### `normalize_for_api()` — `messages/normalize.py:6`

```python
def normalize_for_api(messages: list[Message]) -> list[dict]:
```

Converts the internal `Message` list into the `{"role": str, "content": list[dict]}` format the LLM API accepts.

**Filters** (applied in order):

1. Exclude `is_meta=True` messages — synthetic messages internal to the loop are not sent to the API.
2. Exclude messages with an empty `role` — system-internal messages have no wire role.
3. Exclude messages left with no content after block conversion (an empty `content`, or only blocks that convert to nothing) — the API rejects empty content.

**Block conversion rules** (`_convert_content_blocks()`):

| Block Type | Output Fields |
|---|---|
| `text` | `{"type":"text", "text":...}` (omitted if `text` is `None`) |
| `tool_use` | `{"type":"tool_use", "id":..., "name":..., "input":...}` |
| `tool_result` | `{"type":"tool_result", "tool_use_id":..., "content":...}` (content passed as-is — string or block array; adds `"is_error":True` if is_error=True) |
| `thinking` | `{"type":"thinking", "thinking":...}` (echoes the `text` field under the `thinking` key) |

> **Verbatim echo of thinking blocks**: thinking blocks must be returned to the API as-is, without omission. If even one intermediate thinking block is missing, the API rejects that turn's conversation structure. This rule is also stated separately in [06-invariants](06-invariants.md).

---

## ④ Public API

The loop (`core/loop.py`), orchestrator (`tools/orchestrator.py`), and engine (`core/engine.py`, for the compaction summary) directly import and use the first 3 symbols; the 2 turn-local reminder protocol symbols are shared by the reminder producers (`core/loop.py`·`memory/store.py`) and the detector (`api/anthropic_provider.py`).

| Symbol | Location | Role |
|---|---|---|
| `create_user_message()` | `messages/types.py:81` | Create user input · meta messages |
| `create_tool_result_message()` | `messages/types.py:98` | Create tool result messages |
| `normalize_for_api()` | `messages/normalize.py:6` | Conversion just before API transmission |
| `wrap_system_reminder()` | `messages/types.py:136` | Wrap turn-local reminders in `<system-reminder>` (shared by all producers) |
| `SYSTEM_REMINDER_PREFIX` | `messages/types.py:133` | Detection contract for the Anthropic adapter's cache breakpoint skip |

---

## ⑤ Dependencies

The `messages/` package has no external dependencies (pure standard dataclasses + type conversion).
Conversely, the subsystems below depend on this package:

| Dependency Module | Symbols Used |
|---|---|
| `friday_agent/core/loop.py` | `normalize_for_api()` |
| `friday_agent/core/engine.py` | `normalize_for_api()`, `create_user_message()` (the compaction summary message) |
| `friday_agent/tools/orchestrator.py` | `create_tool_result_message()` |

> Inside the library, `create_user_message()` is used only by `engine.compact()`.

---

## ⑥ Maintenance Notes

- **Role alternation rule**: `tool_result` messages must have `role="user"`, and the first message of the conversation must also be user. `create_tool_result_message()` enforces this. On violation, the API rejects the request. See [06-invariants](06-invariants.md) for the detailed rules.
- **Verbatim thinking echo**: the `thinking` block conversion in `normalize_for_api()` must never be omitted or altered. If missing, the API turn breaks ([06-invariants](06-invariants.md)).
- **tool_result `<tool_use_error>` wrapping**: `create_tool_result_message(is_error=True)` wraps the `content` text in `<tool_use_error>...</tool_use_error>` (`messages/types.py:110`). This is a convention that lets the LLM recognize the error context, so do not change the tag arbitrarily.
- **Single definition of the `<system-reminder>` tag**: the turn-local reminder tag is defined in exactly one place — `wrap_system_reminder()`/`SYSTEM_REMINDER_PREFIX` in `messages/types.py`. The producers (todo · memory index) and the Anthropic adapter's breakpoint-skip detection share this constant, so re-duplicating the literal silently breaks the cache skip.
- **Do not abuse the `is_meta` flag**: `is_meta=True` messages are transparently removed in `normalize_for_api()`. Using it for anything other than loop-internal synthetic messages can drop messages that should reach the API.
- **Name collision**: importing `ContentBlock` from `messages/types.py` and `ContentBlock` (Union alias) from `api/provider.py` in the same file causes a name collision. Disambiguate with an `as` alias when needed.

---

## ⑦ Design Rationale (Why)

**Choosing a single flat dataclass**: `ContentBlock` is designed as a single flat dataclass rather than separate classes per block kind because the loop · orchestrator can then branch on block kind with a single `type` string, keeping pattern matching simple. It is a deliberate choice that accepts the tradeoff of most fields being `None`.

**Separating `normalize_for_api()`**: separating the internal representation from the API wire format means that even if the LLM vendor changes, the `Message`/`ContentBlock` types stay and only the conversion layer is swapped. Vendor-specific rules, such as verbatim echo of `thinking` blocks, are also localized to the conversion layer.
