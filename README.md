# friday-agent-loop

Built by analyzing how several agent loops (agentic loops) work, this is a **domain-agnostic + LLM-agnostic** SDK
for **running agent loops on the cloud/server side**.
The goal is to serve as a foundation for building AI agents for a wide range of purposes beyond programming.

The core is a turn loop that the caller drives by repeatedly calling `FridayAgent.step()`:

```
User message → LLM call → stop_reason branch
                              ├─ end_turn  → stop (Terminal)
                              └─ tool_use  → run tools → append tool_result to conversation → loop again
                                            (ContextOverflowError → caller runs engine.compact(state) → retry)
```

> `docs/architecture/` is the implementation-centric source of truth. For the architecture big picture,
> see `CLAUDE.md` and [docs/architecture/00-overview.md](docs/architecture/00-overview.md).

---

## Requirements

- **Python 3.11+**
- **Anthropic or OpenAI API key** (if you want to make real LLM calls. Unit tests run without a key)

## Installation

```bash
# 1. Virtual environment (recommended)
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 2. Install package + dev dependencies (editable)
pip install -e ".[dev]"
```

Installed dependencies: `anthropic`, `openai`, `pydantic`, `anyio`, `python-dotenv` (+ dev: `pytest`, `pytest-asyncio`).

## API Key Injection

API keys are **not read from environment variables inside the library (`friday_agent`)** — they are always
**injected externally** via `api_key=` into the provider constructor (adapter) (required). Where to get the
key from (environment variables·secret manager, etc.) is the calling application's responsibility. `FridayAgent`
takes the provider built this way directly (no model/api_key arguments).

```python
import os
from friday_agent.api.anthropic_provider import AnthropicProvider
from friday_agent.core.engine import FridayAgent

provider = AnthropicProvider(model="claude-sonnet-4-6", api_key=os.environ["ANTHROPIC_API_KEY"])
engine = FridayAgent(provider=provider, ...)
```

The verification scripts (`scripts/verify_*.py`, `run_agent.py`) act as this "boundary layer",
and `scripts/_env.py` provides `resolve_api_key(model)` (reads the key matching the prefix from `.env`/environment variables) and
`create_provider(model, api_key=...)` (routes to an adapter by prefix). Automatic prefix
routing is the responsibility of this boundary layer, not the library.

```bash
# .env (for running scripts — read by _env.py, not by the library)
ANTHROPIC_API_KEY=sk-ant-...     # when using claude-* models
OPENAI_API_KEY=sk-...            # when using gpt-* models
```

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | For Anthropic (claude-*) calls — read by the scripts' `_env.resolve_api_key` |
| `OPENAI_API_KEY` | For OpenAI (gpt-*) calls — same |

> The verification scripts and `run_agent.py` take the model ID via the `LLM_MODEL` environment variable.

Settings other than keys are also specified as **constructor arguments, not environment variables** (can be omitted to use defaults):

| Setting | Location | Default | Description |
|---|---|---|---|
| `api_key` | Adapter (`AnthropicProvider(api_key=...)` / `OpenAIProvider(api_key=...)`) | (none, required) | Vendor API key. External injection only (no environment-variable fallback). Injected when the provider is created |
| `model` | Adapter (`AnthropicProvider(model=...)`) | (none) | Model ID. If automatic prefix routing is needed, the caller handles it (e.g. `create_provider` in `scripts/_env.py`) |
| `config` | `FridayAgent(config=...)` (e.g. `AnthropicConfig(max_tokens=...)` / `provider.config_type(max_tokens=...)`) | `provider.config_type()` | Inject the vendor call config directly |
| `max_tokens` | Vendor config field (e.g. `provider.config_type(max_tokens=...)`) | `16384` | Max output tokens per response |
| `max_concurrency` | `FridayAgent(max_concurrency=...)` | `10` | Max concurrency for parallel tool execution |

---

## Quick Start

The smallest agent loop — put in a single user message, and the caller drives `step()` repeatedly until the model
calls a tool and then finishes with a final answer.

```python
from friday_agent.core.engine import FridayAgent
from friday_agent.core.state import LoopState, Terminal
from friday_agent.messages.types import create_user_message
from friday_agent.api.provider import ContextOverflowError

engine = FridayAgent(provider=provider, tools=[...])   # TodoWrite is auto-registered as a built-in; memory is opt-in (memory=store)
state = LoopState(messages=[create_user_message("Question")])
while True:
    try:
        outcome = None
        async for item in engine.step(state):   # run one turn — yields each Message as soon as it arrives
            if isinstance(item, (LoopState, Terminal)):
                outcome = item
            else:
                print(item)                     # handle assistant responses / tool_result in real time
    except ContextOverflowError:
        state = await engine.compact(state)     # summarize the conversation, then retry
        continue
    if isinstance(outcome, Terminal):
        break                                   # stop
    state = outcome                             # next turn (use the LoopState as-is)
```

If the last item received is a `Terminal`, stop the loop. `terminal.reason` is one of `completed` / `model_error`, and `terminal.state` is the state to keep — append the next user message to it to continue the conversation, or pass it to `step()` again to retry a `model_error`.

### Injecting Domain Requirements into Compaction (opt-in)

The summarization call in `engine.compact()` runs with a dedicated summarizer system prompt, so `system_prompt` does not reach it. "What must the summary always retain in this domain" is passed via the constructor.

```python
engine = FridayAgent(
    provider=provider,
    system_prompt=DOMAIN_PROMPT,
    compact_instructions=(
        "- Preserve active search filters verbatim.\n"
        "- Replace section 3 with a list of candidate IDs instead of code excerpts."
    ),
)
```

The injected block is placed **after** the default prompt's 9-section spec and **before** the output-format instructions, with a header stating it "takes precedence over the generic sections" — covering both adding sections and redefining existing ones. If omitted (the default), the prompt is byte-for-byte unchanged. For details, see [04-context-compaction](docs/architecture/04-context-compaction.md).

---

## Built-in Capabilities

The two capabilities `FridayAgent` provides at the SDK level. If you pass a tool with the same name via `tools=`, `__init__` rejects it with `ValueError`.

### TODO Tracking (always-on)

The `TodoWrite` tool is always registered, and the todo usage guidance (`TODO_GUIDANCE`) is always appended to the system prompt (no opt-out). It lets the model plan and track progress on multi-step tasks. The tracking list lives in `LoopState.todos` and is re-injected into the API view every turn as a `<system-reminder>` (non-persistent — regenerated deterministically from `todos` on distributed resume). For details, see [02-tool-orchestration](docs/architecture/02-tool-orchestration.md).

### Persistent Memory (opt-in)

Persists typed facts (user/feedback/project/reference) long-term across session boundaries. It is mounted only when a store is explicitly injected, as in `FridayAgent(..., memory=FileMemoryStore())` (not mounted with the default `memory=None`). When mounted, the tools `memory_save`/`memory_read`/`memory_delete` are registered, the memory instructions (`MEMORY_INSTRUCTIONS`) are injected into the system prompt, and the live index is injected every turn as a turn-local `<system-reminder>` (non-persistent — on distributed resume the index freshly reflects the store state at resume time).

For how to implement a store on your own backend (external Storage·DB, etc.), see [Extension Guide — ③ Memory Backend](#extension-guide-customizing-via-interface-injection).

---

## Extension Guide: Customizing via Interface Injection

`FridayAgent` has **3 injection seams** that can be swapped without touching the core loop (`run_one_turn`) — tools·LLM·memory. Each seam only requires implementing a defined interface. Common rules: **inject via the constructor** · if tool names collide, `FridayAgent.__init__` raises `ValueError` · `provider`/`memory` are not serialized into `LoopState`; they are re-injected container-locally.

| seam | Interface to implement | Location | Injection |
|---|---|---|---|
| Tools | Subclass `Tool` | `friday_agent/tools/base.py` | `FridayAgent(tools=[...])` |
| LLM | Subclass `LLMProvider` | `friday_agent/api/provider.py` | `FridayAgent(provider=...)` |
| Memory | Subclass `MemoryStore` | `friday_agent/memory/store.py` | `FridayAgent(memory=...)` |

### ① Tools (Tool)

`ExampleTool` (`friday_agent/tools/builtin/example_tool.py`) is an example of the tool-authoring pattern.
Just subclass `Tool` and implement the input schema and `call()`.

```python
from pydantic import BaseModel, Field
from friday_agent.tools.base import Tool, ToolResult


class WeatherInput(BaseModel):
    city: str = Field(description="City to look up the weather for")   # ← field descriptions are also passed to the model


class WeatherTool(Tool):
    """Looks up the current weather for a given city. Use when the user asks about the weather/temperature in a specific location."""
    # ↑ this docstring becomes, as-is, the tool description sent to the LLM — do not leave it empty

    name = "WeatherTool"

    def input_schema(self) -> type[BaseModel]:
        return WeatherInput

    def is_concurrency_safe(self, input_data: dict) -> bool:
        return True   # read-only, so safe to run in parallel

    async def call(self, args: dict) -> ToolResult:
        parsed = WeatherInput(**args)
        # implement the actual API/DB call here
        return ToolResult(data=f"Sunny in {parsed.city}")
```

Pass the tool you built to `FridayAgent(tools=[WeatherTool()])` and the model can call it.

> `TodoWrite` (always) and the memory tools (`memory_save`/`memory_read`/`memory_delete`, when `memory=` is mounted) are registered by the SDK, so do not put them in `tools=` yourself — if the names collide, `FridayAgent.__init__` rejects them with `ValueError` (see [Built-in Capabilities](#built-in-capabilities)).

#### How the LLM Recognizes Tools

Everything the model relies on to judge "what this tool is and how to call it" reduces to the **3 keys** built by `Tool.get_tool_schema()`
(`friday_agent/tools/base.py`). The schema that the `WeatherTool` above actually
sends is as follows (passed to the API as-is):

```jsonc
{
  "name": "WeatherTool",                       // ① call identifier — the class's name attribute
  "description": "Looks up the current weather ...",  // ② what the tool does — the class docstring
  "input_schema": {                            // ③ what arguments it needs — the Pydantic model from input_schema()
    "properties": {
      "city": { "description": "City to look up the weather for", "title": "City", "type": "string" }
    },
    "required": ["city"],
    "type": "object"
  }
}
```

So getting the LLM to recognize a tool "well" comes down to **writing these three things carefully**:

| What | Where it comes from | Developer guide |
|---|---|---|
| `name` | Class `name` attribute | Verb+noun so the intent is clear (`SearchOrders` > `Tool1`) |
| `description` | **Class docstring** | **Always fill it in.** "What it does + when to call it" in one or two sentences. If empty, an empty string is sent and the model must infer from the name alone |
| `input_schema` | Pydantic model returned by `input_schema()` | Put `Field(description=...)` on every field. Types·required·defaults are serialized automatically by Pydantic |

> To change the description dynamically based on input instead of a static docstring, override the `description()` method (defaults to the docstring).
> The top-level `title` is removed, and `$defs` are expanded inline and then removed (nested models·enums are exposed as-is without `$ref` — e.g. `status` enum values appear directly in the schema). As shown above, **per-field `title` remains** — this is expected.

#### Execution Policy Methods Are Not Sent to the LLM

`is_concurrency_safe` is **not included** in
the schema. It is not for the model's awareness — it is a runtime signal by which **the orchestrator controls execution**:

> Only tools whose `is_concurrency_safe()` is `True` run in a parallel batch (read-only tools are the typical example).
> Tools that change external state (mutating) return `False` from `is_concurrency_safe()` and run sequentially.
> For tool partitioning details, see [02-tool-orchestration](docs/architecture/02-tool-orchestration.md).

### ② LLM Backend (LLMProvider)

Swapping the LLM only requires implementing **`LLMProvider`** (`friday_agent/api/provider.py`). Register a `config_type` (the config class) and have `complete()` **normalize** the vendor response into `AssistantResponse`, and a different backend works with no changes to the core loop.

```python
from dataclasses import dataclass
from friday_agent.api.provider import (
    LLMProvider, AssistantResponse, StopReason,
    TextBlock, ToolUseBlock, TokenUsage, ContextOverflowError,
)


@dataclass
class MyConfig:                       # minimal fields — max_tokens, temperature (LLMConfig protocol)
    max_tokens: int = 16384
    temperature: float | None = None


class MyProvider(LLMProvider[MyConfig]):
    config_type = MyConfig            # ← required: used to create·validate the default config

    async def complete(self, messages, system_prompt, tools, config) -> AssistantResponse:
        resp = await call_my_backend(messages, system_prompt, tools, config)   # vendor call
        # vendor response → normalize to AssistantResponse
        return AssistantResponse(
            content=[TextBlock(text=resp.text)],     # or ToolUseBlock(id, name, input=<dict>)
            stop_reason=StopReason.END_TURN,         # TOOL_USE / MAX_TOKENS / END_TURN
            usage=TokenUsage(input_tokens=resp.in_, output_tokens=resp.out),
        )
        # on context overflow, raise ContextOverflowError → caller runs engine.compact(state), then retries
```

Implementation contract:

- **Setting `config_type`** + **implementing `complete()`** is the whole core. Always normalize the response to `AssistantResponse(content, stop_reason, usage, id="", model="")`.
- **content blocks**: `TextBlock(text)` · `ToolUseBlock(id, name, input=<already-parsed dict>)` · `ThinkingBlock` (some backends). `tool_use.input` must be a dict, not a string.
- **`stop_reason`**: `END_TURN` / `TOOL_USE` / `MAX_TOKENS` / `CONTEXT_WINDOW_EXCEEDED`. Vendor values with no mapping go to `END_TURN`.
- **Exception mapping**: map vendor exceptions into the 5-class hierarchy (`LLMError`·`RateLimitError`·`ContextOverflowError`·`AuthError`·`TransientError`). Context overflow **propagates** as `ContextOverflowError` (the loop does not catch it; the caller runs `compact`).
- **Vendor rules are the adapter's responsibility**: omit the API field entirely for empty `tools`, do not send `temperature` when thinking is enabled, etc.

For the vendor differences table·normalization details, see [03-llm-providers](docs/architecture/03-llm-providers.md).

### ③ Memory Backend (MemoryStore)

A single `MemoryStore` **owns both the persistent backend and its tool surface (`tools()`)**. The store injected via `memory=` is itself the entire mounted subsystem (the file-based default implementation is `FileMemoryStore`).

```python
from friday_agent.memory.store import MemoryStore, MemoryEntry, IndexEntry
from friday_agent.core.engine import FridayAgent


class RedisMemoryStore(MemoryStore):
    async def save(self, entry: MemoryEntry) -> None: ...       # upsert by name (same name → update)
    async def read(self, name: str) -> MemoryEntry | None: ...  # full entry (including body) or None
    async def delete(self, name: str) -> None: ...              # ignore if missing
    async def load_index(self) -> list[IndexEntry]:             # metadata only, no body (for index injection)
        ...
    # tools() as inherited by default gives memory_save/read/delete as-is.
    # To change the tool surface (e.g. add search), override it:
    #   def tools(self): return [MemorySave(self), MemoryRead(self), MySearchTool(self)]


engine = FridayAgent(provider=provider, memory=RedisMemoryStore())   # replaces store+tools wholesale
```

Implementation contract:

- **Implement 4 async methods**: `save` (upsert) · `read` · `delete` (ignore if missing) · `load_index` (metadata only, no body).
- **Data model**: `MemoryEntry(name, description, type: MemoryType, body, updated_at)` · `IndexEntry` (no body) · `MemoryType` = `user`/`feedback`/`project`/`reference`.
- **`tools()`** default = `memory_save`/`memory_read`/`memory_delete` wrappers. Overriding it replaces the tool surface wholesale.
- The default `FileMemoryStore` (→ `FRIDAY_MEMORY.md`) is a single-process/local convenience — for distributed/cloud persistence, inject a store backed by external Storage (re-injected container-locally just like `provider`, not serialized into `LoopState`).

For details, see [08-memory](docs/architecture/08-memory.md).

---

## Distributed Resume (stateless)

To **split a multi-turn run turn by turn and distribute it across multiple containers**, use `step()` and the serialization API — one turn = one unit, and `LoopState` = the only state that crosses container boundaries.

```python
import json
from friday_agent.core.state import LoopState, Terminal
from friday_agent.messages.types import create_user_message

# Container A — first turn
state = LoopState(messages=[create_user_message("Question")])
outcome = None
async for item in engine.step(state):           # run one turn
    if isinstance(item, (LoopState, Terminal)):
        outcome = item
    else:
        persist(item)                           # handle Messages in real time
if isinstance(outcome, LoopState):
    blob = json.dumps(outcome.to_dict())        # serialize the final LoopState sentinel to store/transport

# Container B (a different system) — load the blob and continue
state = LoopState.from_dict(json.loads(blob))
async for item in engine.step(state):           # next turn … repeat until Terminal
    ...
```

Because the loop state is only ever updated at clean turn boundaries (preserving `tool_use`↔`tool_result` integrity), `LoopState` can be serialized and resumed as-is. For design details, see [01-core-loop](docs/architecture/01-core-loop.md).
