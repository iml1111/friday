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

## 🧭 Implementation Guide — for Code Review

All the core implementation lives in `friday_agent/`. The entry point is `friday_agent/core/engine.py` › `FridayAgent.step(state)` / `FridayAgent.compact(state)`, and the heart of the loop is `friday_agent/core/loop.py` › `run_one_turn()`.

One pass of the turn loop:

```
run_one_turn() once
 1. normalize_for_api() → provider.complete() call
 2. stop_reason branch:
      end_turn  → Terminal(completed)
      tool_use  → run_tools() → append tool_result → next turn (LoopState)
 ContextOverflowError → caller runs engine.compact(state) → retry
 LLMError → Terminal(model_error) + tool_result backfill
```

For structural details (reading order·key data structures·invariants·module map), see [docs/architecture/](docs/architecture/00-overview.md).

For the detailed list of pitfalls, see `CLAUDE.md` (Key Implementation Pitfalls) and [docs/architecture/06-invariants.md](docs/architecture/06-invariants.md).
For swapping the LLM backend, see [Swapping In a Different LLM Backend](#swapping-in-a-different-llm-backend) below,
and for adding tools, see [Writing a Custom Tool (BYO Tool)](#writing-a-custom-tool-byo-tool).

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

engine = FridayAgent(provider=provider, tools=[...])
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

If the last item received is a `Terminal`, stop the loop. `terminal.reason` is one of `completed` / `model_error`.

---

## Running the Verification Scripts (Real API)

`scripts/` contains scripts that run each Phase's verification scenario against the real Anthropic API.
**These call the real API, so they incur token costs** (each script is guardrailed with `max_tokens`).

```bash
# Inject the model ID via the LLM_MODEL environment variable (provider auto-routed by prefix)
# P2 — single tool cycle: tool_use → tool_result → final response
LLM_MODEL=claude-sonnet-4-6 python scripts/verify_p2.py

# P3 — caller-driven compact recovery verification (explicitly calls engine.compact() to check real-backend summarization+continuity)
LLM_MODEL=claude-sonnet-4-6 python scripts/verify_p3.py

# P4 — real backend end-to-end + adapter swap structure demonstration
LLM_MODEL=claude-sonnet-4-6 python scripts/verify_p4.py
```

Each script prints a checklist and returns an exit code (0/1) along with PASS/FAIL.

## Running Tests

Unit/deterministic tests run **without an API key** (using a fake provider).

```bash
pytest
# or verbose
pytest -v
```

---

## Writing a Custom Tool (BYO Tool)

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

### How the LLM Recognizes Tools

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
> Of the Pydantic v2 meta-keys, only the top-level `title`/`$defs` are removed; as shown above, **per-field `title` remains** — this is expected.

### Execution Policy Methods Are Not Sent to the LLM

`is_concurrency_safe` is **not included** in
the schema. It is not for the model's awareness — it is a runtime signal by which **the orchestrator controls execution**:

> Only tools whose `is_concurrency_safe()` is `True` run in a parallel batch (read-only tools are the typical example).
> Tools that change external state (mutating) return `False` from `is_concurrency_safe()` and run sequentially.
> For tool partitioning details, see [02-tool-orchestration](docs/architecture/02-tool-orchestration.md).

---

## Package Structure (Module Map)

All core code lives under `friday_agent/`. For each file's responsibility and entry symbols, see [the Module Map in 00-overview](docs/architecture/00-overview.md#module-map).

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

## Swapping In a Different LLM Backend

Swapping the LLM only requires implementing the single interface **`LLMProvider`** (`friday_agent/api/provider.py`).
Once `complete()` normalizes the LLM response into `AssistantResponse`, a different backend works without changing the core loop code.
For abstraction boundary details, see [03-llm-providers](docs/architecture/03-llm-providers.md).

---

## Further Reading

- `CLAUDE.md` — architecture big picture, Implementation Scope Charter, key pitfalls
- `docs/architecture/` — implementation-centric architecture docs (source of truth)
