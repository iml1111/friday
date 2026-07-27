"""Render the EXACT Anthropic wire payload for every LLM call the engine makes.

Nothing here is hand-written prose about what the payload "should" look like: a real
AnthropicProvider runs with an injected fake client, so what gets dumped is literally
the dict that would have been passed to `client.messages.create(**params)` — including
cache_control breakpoints, turn-local reminders, and the compaction prompt.

Scenario driven end to end:
  CALL 1  step() turn 1        — cold start, user question only
  CALL 2  step() turn 2        — after a parallel TodoWrite + domain-tool batch
  CALL 3  compact()            — summarization call (different system, no tools)
  CALL 4  step() after compact — resumed from the summary message

No API key and no network: the provider is real, only its client is faked.

Usage (from the repo root):
    python scripts/dump_wire_payload.py docs/architecture/wire

Change the scenario — domain system prompt, tools, memories, compact_instructions —
via the constants under "Scenario fixtures" below, then re-run to refresh the dumps.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

# Add the repo root to sys.path so friday_agent can be imported without an editable install.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday_agent.api.anthropic_provider import AnthropicProvider
from friday_agent.api.configs import AnthropicConfig
from friday_agent.core.engine import FridayAgent
from friday_agent.core.state import LoopState, Terminal
from friday_agent.memory.store import FileMemoryStore, MemoryEntry, MemoryType
from friday_agent.messages.types import create_user_message
from friday_agent.tools.builtin.example_tool import ExampleTool

MODEL = "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# Scenario fixtures
# ---------------------------------------------------------------------------

# The caller-supplied tool is the SDK's own ExampleTool (friday_agent/tools/builtin/
# example_tool.py) — it is NOT auto-registered, so passing it via tools=[] is exactly
# what a real caller does with a domain tool.
CALLER_TOOLS = [ExampleTool()]

DOMAIN_SYSTEM_PROMPT = (
    "You are a helpful assistant. When the user asks you to process some text, call "
    "ExampleTool with that text as the payload, then report the tool's result."
)

COMPACT_INSTRUCTIONS = (
    "- Preserve every ExampleTool payload that was processed, verbatim.\n"
    "- For section 3, list the payloads and their processed results instead of code excerpts."
)


# ---------------------------------------------------------------------------
# Fake Anthropic client — captures params, replays scripted native responses
# ---------------------------------------------------------------------------

def _native(blocks: list[dict], stop_reason: str):
    ns = []
    for b in blocks:
        ns.append(SimpleNamespace(**b))
    return SimpleNamespace(
        content=ns,
        stop_reason=stop_reason,
        id="msg_dump",
        model=MODEL,
        usage=SimpleNamespace(input_tokens=0, output_tokens=0),
    )


SCRIPT = [
    # CALL 1 → plan, then fan out a read-only and a mutating ExampleTool call.
    # ExampleTool.is_concurrency_safe() keys off `mutating`, so the orchestrator
    # partitions these into separate batches — visible as separate tool_result
    # messages in the CALL 2 dump.
    _native(
        [
            {"type": "text", "text": "두 건을 처리한다."},
            {"type": "tool_use", "id": "toolu_01", "name": "TodoWrite",
             "input": {"todos": [
                 {"content": "입력 텍스트 2건 처리", "status": "in_progress"},
                 {"content": "처리 결과 보고", "status": "pending"},
             ]}},
            {"type": "tool_use", "id": "toolu_02", "name": "ExampleTool",
             "input": {"payload": "first document", "mutating": False}},
            {"type": "tool_use", "id": "toolu_03", "name": "ExampleTool",
             "input": {"payload": "second document", "mutating": True}},
        ],
        "tool_use",
    ),
    # CALL 2 → final answer
    _native([{"type": "text", "text": "두 건 모두 처리됐다: first document, second document."}],
            "end_turn"),
    # CALL 3 → compaction summary
    _native(
        [{"type": "text", "text": "<analysis>scratch</analysis><summary>"
                                 "ExampleTool로 'first document'(read-only)와 "
                                 "'second document'(mutating)를 처리했고 둘 다 "
                                 "'processed: ...'로 반환됐다."
                                 "</summary>"}],
        "end_turn",
    ),
    # CALL 4 → post-compaction turn
    _native([{"type": "text", "text": "다음 문서를 기다린다."}], "end_turn"),
]


class _Messages:
    def __init__(self, sink: list[dict]) -> None:
        self._sink = sink
        self._i = 0

    async def create(self, **params):
        self._sink.append(json.loads(json.dumps(params, ensure_ascii=False)))
        resp = SCRIPT[self._i]
        self._i += 1
        return resp


class FakeClient:
    def __init__(self, sink: list[dict]) -> None:
        self.messages = _Messages(sink)


# ---------------------------------------------------------------------------
# Rendering — final prompt exactly as submitted, as Markdown
# ---------------------------------------------------------------------------

CALL_META = [
    ("CALL 1 — engine.step() · 턴 1",
     "호출자가 만든 LoopState(messages=[user]) 하나로 시작하는 첫 API 호출."),
    ("CALL 2 — engine.step() · 턴 2",
     "TodoWrite + ExampleTool 2건의 tool_result가 들어온 뒤의 호출."),
    ("CALL 3 — engine.compact() · 요약 호출",
     "컨텍스트 오버플로 복구용 요약 호출. system·tools 모두 턴 루프와 다르다."),
    ("CALL 4 — engine.step() · 컴팩션 후 재개",
     "messages가 요약 메시지 1개로 축소된 상태에서의 첫 호출."),
]


def fence(text: str) -> str:
    """Fence verbatim text with a backtick run longer than any inside it."""
    longest = 0
    run = 0
    for ch in text:
        run = run + 1 if ch == "`" else 0
        longest = max(longest, run)
    bar = "`" * max(3, longest + 1)
    return f"{bar}text\n{text}\n{bar}"


def json_block(obj) -> str:
    return "```json\n" + json.dumps(obj, ensure_ascii=False, indent=2) + "\n```"


def _cc(block: dict) -> str:
    cc = block.get("cache_control")
    return f"  ·  `cache_control={json.dumps(cc)}`" if cc else ""


def render(idx: int, params: dict) -> str:
    title, subtitle = CALL_META[idx]
    o: list[str] = []

    o.append(f"# {title}")
    o.append("")
    o.append(subtitle)
    o.append("")
    o.append("> 아래는 `client.messages.create(**params)`에 실제로 들어가는 값 그대로다.")
    o.append("> API 직렬화 순서(`tools` → `system` → `messages`)로 편다.")
    o.append("")

    # --- scalars ---
    o.append("## 호출 파라미터")
    o.append("")
    o.append("| key | value |")
    o.append("|---|---|")
    for key in ("model", "max_tokens", "stream", "temperature", "thinking"):
        if key in params:
            o.append(f"| `{key}` | `{params[key]!r}` |")
    o.append(f"| `tools` | {len(params['tools'])}개 |" if "tools" in params
             else "| `tools` | *(필드 없음 — 빈 리스트는 통째로 생략)* |")
    o.append(f"| `system` | 블록 {len(params['system'])}개 |")
    o.append(f"| `messages` | 메시지 {len(params['messages'])}개 |")
    o.append("")

    # --- tools ---
    o.append("## `tools`")
    o.append("")
    if "tools" not in params:
        o.append("*이 호출에는 `tools` 필드가 없다.*")
        o.append("")
    else:
        for t in params["tools"]:
            o.append(f"### `{t['name']}`")
            o.append("")
            o.append("**description** (모델이 보는 텍스트):")
            o.append("")
            o.append(fence(t["description"]))
            o.append("")
            o.append("**input_schema**:")
            o.append("")
            o.append(json_block(t["input_schema"]))
            o.append("")

    # --- system ---
    o.append("## `system`")
    o.append("")
    for i, b in enumerate(params["system"]):
        o.append(f"### 블록 {i} — {len(b.get('text', '')):,} chars{_cc(b)}")
        o.append("")
        o.append(fence(b.get("text", "")))
        o.append("")

    # --- messages ---
    o.append("## `messages`")
    o.append("")
    n = len(params["messages"])
    for i, msg in enumerate(params["messages"]):
        rel = f" *(= `messages[{i - n}]`)*" if i >= n - 2 else ""
        content = msg["content"]

        if isinstance(content, str):
            o.append(f"### `messages[{i}]` — role: `{msg['role']}` · content: 문자열 {len(content):,} chars{rel}")
            o.append("")
            o.append(fence(content))
            o.append("")
            continue

        o.append(f"### `messages[{i}]` — role: `{msg['role']}` · 블록 {len(content)}개{rel}")
        o.append("")
        for j, b in enumerate(content):
            t = b.get("type")
            if t == "text":
                kind = "text" + ("  ·  *turn-local `<system-reminder>` (LoopState 비영속)*"
                                 if str(b.get("text", "")).startswith("<system-reminder>") else "")
                o.append(f"**[{j}] {kind}**{_cc(b)}")
                o.append("")
                o.append(fence(b.get("text", "")))
            elif t == "tool_use":
                o.append(f"**[{j}] tool_use** · `name={b.get('name')}` · `id={b.get('id')}`{_cc(b)}")
                o.append("")
                o.append(json_block(b.get("input")))
            elif t == "tool_result":
                err = " · `is_error=True`" if b.get("is_error") else ""
                o.append(f"**[{j}] tool_result** · `tool_use_id={b.get('tool_use_id')}`{err}{_cc(b)}")
                o.append("")
                o.append(fence(str(b.get("content"))))
            else:
                o.append(f"**[{j}] {t}**{_cc(b)}")
                o.append("")
                o.append(json_block(b))
            o.append("")

    # --- raw ---
    o.append("---")
    o.append("")
    o.append("<details>")
    o.append("<summary>RAW — messages.create()에 넘어가는 dict 전체 (JSON)</summary>")
    o.append("")
    o.append(json_block(params))
    o.append("")
    o.append("</details>")
    o.append("")
    return "\n".join(o)


# ---------------------------------------------------------------------------
# Drive
# ---------------------------------------------------------------------------

async def main(outdir: Path) -> None:
    captured: list[dict] = []
    provider = AnthropicProvider(api_key="dummy-not-used", model=MODEL,
                                 client=FakeClient(captured))

    with tempfile.TemporaryDirectory() as tmp:
        store = FileMemoryStore(path=str(Path(tmp) / "FRIDAY_MEMORY.md"))
        await store.save(MemoryEntry(
            name="batch-size-limit",
            description="한 번에 처리하는 문서는 2건까지",
            type=MemoryType.project,
            body="처리 배치는 2건을 넘기지 않는다. **Why:** 다운스트림 큐가 그 이상을 못 받는다.",
        ))
        await store.save(MemoryEntry(
            name="prefers-korean",
            description="사용자는 한국어 응답을 선호한다",
            type=MemoryType.user,
            body="처리 결과 보고는 한국어로.",
        ))

        engine = FridayAgent(
            provider=provider,
            tools=CALLER_TOOLS,
            system_prompt=DOMAIN_SYSTEM_PROMPT,
            config=AnthropicConfig(max_tokens=1024),
            memory=store,
            compact_instructions=COMPACT_INSTRUCTIONS,
        )

        # --- CALL 1 & 2: run the turn loop to completion --------------------
        state = LoopState(messages=[create_user_message("이 문서 두 개 처리해줘")])
        while True:
            outcome = None
            async for item in engine.step(state):
                if isinstance(item, (LoopState, Terminal)):
                    outcome = item
            if isinstance(outcome, Terminal):
                break
            state = outcome

        # --- CALL 3: compaction --------------------------------------------
        # Rebuild the pre-terminal state so compact() sees a real conversation.
        compacted = await engine.compact(state)

        # --- CALL 4: resumed turn ------------------------------------------
        async for item in engine.step(compacted):
            pass

    outdir.mkdir(parents=True, exist_ok=True)
    names = [
        "01-step-turn1.md",
        "02-step-turn2-after-tools.md",
        "03-compact-call.md",
        "04-step-after-compact.md",
    ]
    for i, (params, name) in enumerate(zip(captured, names)):
        (outdir / name).write_text(render(i, params), encoding="utf-8")
        print(f"wrote {name}  ({len(render(i, params)):,} chars)")

    # Cross-call cache-prefix proof.
    sysblocks = [json.dumps(c["system"], ensure_ascii=False, sort_keys=True) for c in captured]
    print("\nsystem prefix identical across step() calls 1/2/4:",
          sysblocks[0] == sysblocks[1] == sysblocks[3])
    print("compact() system differs (dedicated summarizer):", sysblocks[2] != sysblocks[0])


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
