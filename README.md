# friday-agent-loop

여러 에이전트 루프(agentic loop) 동작 구조를 분석해, **클라우드/서버 사이드에서 에이전트 루프를 구동**하기 위한
**도메인 무관 + LLM 무관(LLM-agnostic)** SDK입니다.
프로그래밍 외 다양한 목적의 AI 에이전트를 만드는 토대로 삼는 것이 목표입니다.

핵심은 호출자가 `FridayAgent.step()`을 반복해 구동하는 턴 루프입니다:

```
사용자 메시지 → LLM 호출 → stop_reason 분기
                              ├─ end_turn  → 종료(Terminal)
                              └─ tool_use  → 도구 실행 → tool_result를 대화에 추가 → 다시 루프
                                            (ContextOverflowError → 호출자가 engine.compact(state) → 재시도)
```

> `docs/architecture/`가 구현체 중심 진실 공급원입니다. 아키텍처 큰 그림은
> `CLAUDE.md`와 [docs/architecture/00-overview.md](docs/architecture/00-overview.md)를 참고하세요.

---

## 요구 사항

- **Python 3.11+**
- **Anthropic 또는 OpenAI API 키** (실제 LLM 호출을 해보려는 경우. 단위 테스트는 키 없이 동작)

## 설치

```bash
# 1. 가상환경 (권장)
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 2. 패키지 + 개발 의존성 설치 (editable)
pip install -e ".[dev]"
```

설치되는 의존성: `anthropic`, `openai`, `pydantic`, `anyio`, `python-dotenv` (+ dev: `pytest`, `pytest-asyncio`).

## API 키 주입

API 키는 **라이브러리(`friday_agent`) 내부에서 환경변수를 읽지 않습니다** — 항상
provider 생성자(어댑터)에 `api_key=`로 **외부에서 주입**합니다(필수). 키를 어디서
가져올지(환경변수·시크릿 매니저 등)는 호출하는 애플리케이션의 책임입니다. `FridayAgent`은
이렇게 만든 provider를 직접 받습니다(model/api_key 인자 없음).

```python
import os
from friday_agent.api.anthropic_provider import AnthropicProvider
from friday_agent.core.engine import FridayAgent

provider = AnthropicProvider(model="claude-sonnet-4-6", api_key=os.environ["ANTHROPIC_API_KEY"])
engine = FridayAgent(provider=provider, ...)
```

검증 스크립트(`scripts/verify_*.py`, `run_agent.py`)는 이 "경계 계층" 역할을 하며,
`scripts/_env.py`가 `resolve_api_key(model)`(prefix에 맞는 키를 `.env`/환경변수에서 읽음)와
`create_provider(model, api_key=...)`(prefix로 어댑터 라우팅)를 제공합니다. prefix 자동
라우팅은 라이브러리가 아니라 이 경계 계층의 책임입니다.

```bash
# .env (스크립트 실행용 — 라이브러리가 아니라 _env.py가 읽음)
ANTHROPIC_API_KEY=sk-ant-...     # claude-* 모델 사용 시
OPENAI_API_KEY=sk-...            # gpt-* 모델 사용 시
```

| 변수 | 설명 |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic(claude-*) 호출용 — 스크립트의 `_env.resolve_api_key`가 읽음 |
| `OPENAI_API_KEY` | OpenAI(gpt-*) 호출용 — 동일 |

> 검증 스크립트와 `run_agent.py`는 모델 ID를 `LLM_MODEL` 환경변수로 받습니다.

키 외 설정값도 **환경 변수가 아니라 생성자 인자**로 지정합니다(기본값을 쓰면 생략 가능):

| 설정 | 위치 | 기본값 | 설명 |
|---|---|---|---|
| `api_key` | 어댑터(`AnthropicProvider(api_key=...)` / `OpenAIProvider(api_key=...)`) | (없음, 필수) | 벤더 API 키. 외부 주입 전용(환경변수 폴백 없음). provider 생성 시점에 주입 |
| `model` | 어댑터(`AnthropicProvider(model=...)`) | (없음) | 모델 ID. prefix 자동 라우팅이 필요하면 caller가 처리(예: `scripts/_env.py`의 `create_provider`) |
| `config` | `FridayAgent(config=...)` (예: `AnthropicConfig(max_tokens=...)` / `provider.config_type(max_tokens=...)`) | `provider.config_type()` | 벤더 호출 config를 직접 주입 |
| `max_tokens` | 벤더 config 필드 (예: `provider.config_type(max_tokens=...)`) | `16384` | 응답당 최대 출력 토큰 |
| `max_concurrency` | `FridayAgent(max_concurrency=...)` | `10` | 도구 병렬 실행 최대 동시성 |

---

## 빠른 시작 (Quick Start)

가장 작은 에이전트 루프 — 사용자 메시지 하나를 넣고, 모델이 도구를 호출했다가
최종 답변으로 종료하기까지 호출자가 `step()`을 반복해 구동합니다.

```python
from friday_agent.core.engine import FridayAgent
from friday_agent.core.state import LoopState, Terminal
from friday_agent.messages.types import create_user_message
from friday_agent.api.provider import ContextOverflowError

engine = FridayAgent(provider=provider, tools=[...])   # TodoWrite는 빌트인 자동 등록; memory는 opt-in(memory=store)
state = LoopState(messages=[create_user_message("질문")])
while True:
    try:
        outcome = None
        async for item in engine.step(state):   # 한 턴 실행 — 각 Message 도착 즉시 yield
            if isinstance(item, (LoopState, Terminal)):
                outcome = item
            else:
                print(item)                     # 어시스턴트 응답 / tool_result 실시간 처리
    except ContextOverflowError:
        state = await engine.compact(state)     # 대화 요약 후 재시도
        continue
    if isinstance(outcome, Terminal):
        break                                   # 종료
    state = outcome                             # 다음 턴 (LoopState를 그대로 사용)
```

마지막으로 받은 item이 `Terminal`이면 루프를 종료합니다. `terminal.reason`은 `completed` / `model_error` 중 하나입니다.

### 컴팩션에 도메인 요구사항 주입 (opt-in)

`engine.compact()`의 요약 호출은 전용 요약기 system 프롬프트로 돌기 때문에 `system_prompt`가 닿지 않습니다. "이 도메인에서는 요약에 무엇을 반드시 남겨야 하는가"는 생성자로 넘깁니다.

```python
engine = FridayAgent(
    provider=provider,
    system_prompt=DOMAIN_PROMPT,
    compact_instructions=(
        "- 활성 검색 필터는 원문 그대로 보존할 것.\n"
        "- 3번 섹션은 코드 발췌 대신 후보 ID 목록으로 대체할 것."
    ),
)
```

주입된 블록은 기본 프롬프트의 9개 섹션 명세 **뒤**, 출력 형식 지시 **앞**에 들어가며 "generic 섹션보다 우선한다"는 헤더가 붙습니다 — 섹션 추가와 기존 섹션 재정의를 모두 커버합니다. 생략하면(기본값) 프롬프트는 바이트 단위로 그대로입니다. 상세는 [04-context-compaction](docs/architecture/04-context-compaction.md) 참조.

---

## 빌트인 능력

`FridayAgent`가 SDK 수준에서 제공하는 두 능력이다. 같은 이름의 도구를 `tools=`로 넘기면 `__init__`이 `ValueError`로 거부한다.

### TODO 추적 (always-on)

`TodoWrite` 도구가 항상 등록되고, todo 사용 가이던스(`TODO_GUIDANCE`)가 시스템 프롬프트에 항상 덧붙는다(opt-out 없음). 멀티스텝 작업에서 모델이 계획을 세우고 진행을 추적하는 용도다. 추적 리스트는 `LoopState.todos`에 살고, 매 턴 `<system-reminder>`로 API view에 재주입된다(비영속 — 분산 재개 시 `todos`에서 결정론적으로 재생성). 상세는 [02-tool-orchestration](docs/architecture/02-tool-orchestration.md) 참조.

### 영속 메모리 (opt-in)

세션 경계를 넘어 타입화된 fact(user/feedback/project/reference)를 장기화한다. `FridayAgent(..., memory=FileMemoryStore())`처럼 store를 명시 주입해야 장착되며(기본 `memory=None`이면 미장착), 장착 시 도구 `memory_save`/`memory_read`/`memory_delete`가 등록되고 메모리 지침(`MEMORY_INSTRUCTIONS`)이 시스템 프롬프트에, 라이브 인덱스가 매 턴 turn-local `<system-reminder>`로 주입된다(비영속 — 분산 재개 시 인덱스가 재개 시점 store 상태를 fresh 반영).

자신의 백엔드(외부 Storage·DB 등)로 store를 구현하는 방법은 [확장 가이드 — ③ 메모리 백엔드](#확장-가이드-인터페이스-주입으로-커스터마이징) 참조.

---

## 확장 가이드: 인터페이스 주입으로 커스터마이징

`FridayAgent`는 코어 루프(`run_one_turn`)를 건드리지 않고 교체 가능한 **3개 주입 seam**을 가진다 — 도구·LLM·메모리. 각 seam은 정해진 인터페이스만 구현하면 된다. 공통 규칙: **생성자에서 주입** · 도구 이름이 겹치면 `FridayAgent.__init__`이 `ValueError` · `provider`/`memory`는 `LoopState`에 직렬화되지 않고 컨테이너-로컬로 재주입된다.

| seam | 구현 인터페이스 | 위치 | 주입 |
|---|---|---|---|
| 도구 | `Tool` 상속 | `friday_agent/tools/base.py` | `FridayAgent(tools=[...])` |
| LLM | `LLMProvider` 상속 | `friday_agent/api/provider.py` | `FridayAgent(provider=...)` |
| 메모리 | `MemoryStore` 상속 | `friday_agent/memory/store.py` | `FridayAgent(memory=...)` |

### ① 도구 (Tool)

`ExampleTool`(`friday_agent/tools/builtin/example_tool.py`)이 도구 작성 패턴의 예시입니다.
`Tool`을 상속하고 입력 스키마와 `call()`만 구현하면 됩니다.

```python
from pydantic import BaseModel, Field
from friday_agent.tools.base import Tool, ToolResult


class WeatherInput(BaseModel):
    city: str = Field(description="날씨를 조회할 도시 이름")   # ← 필드 설명도 모델에 전달됨


class WeatherTool(Tool):
    """주어진 도시의 현재 날씨를 조회한다. 사용자가 특정 지역의 날씨/기온을 물을 때 사용."""
    # ↑ 이 docstring이 그대로 LLM에 전달되는 도구 설명(description)이 된다 — 비우지 말 것

    name = "WeatherTool"

    def input_schema(self) -> type[BaseModel]:
        return WeatherInput

    def is_concurrency_safe(self, input_data: dict) -> bool:
        return True   # 읽기 전용이라 병렬 안전

    async def call(self, args: dict) -> ToolResult:
        parsed = WeatherInput(**args)
        # 여기에 실제 API/DB 호출을 구현
        return ToolResult(data=f"{parsed.city}는 맑음")
```

만든 도구를 `FridayAgent(tools=[WeatherTool()])`에 넘기면 모델이 호출할 수 있습니다.

> `TodoWrite`(항상)와 메모리 도구(`memory_save`/`memory_read`/`memory_delete`, `memory=` 장착 시)는 SDK가 등록하므로 `tools=`에 직접 넣지 마세요 — 이름이 겹치면 `FridayAgent.__init__`이 `ValueError`로 거부합니다([빌트인 능력](#빌트인-능력) 참조).

#### LLM은 도구를 어떻게 인지하는가

모델이 "이 도구가 무엇이고 어떻게 호출하는지" 판단하는 근거는 전부 `Tool.get_tool_schema()`
(`friday_agent/tools/base.py`)가 만드는 **3개 키**로 환원됩니다. 위 `WeatherTool`이 실제로
전송하는 스키마는 다음과 같습니다(API로 그대로 전달):

```jsonc
{
  "name": "WeatherTool",                       // ① 호출 식별자 — 클래스의 name 속성
  "description": "주어진 도시의 현재 날씨를 ...",  // ② 무엇을 하는 도구인가 — 클래스 docstring
  "input_schema": {                            // ③ 어떤 인자가 필요한가 — input_schema()의 Pydantic 모델
    "properties": {
      "city": { "description": "날씨를 조회할 도시 이름", "title": "City", "type": "string" }
    },
    "required": ["city"],
    "type": "object"
  }
}
```

따라서 도구를 LLM에 "잘" 인지시키는 일은 곧 **이 세 가지를 충실히 쓰는 것**입니다:

| 무엇이 | 어디서 오는가 | 개발자 가이드 |
|---|---|---|
| `name` | 클래스 `name` 속성 | 동사+명사로 의도가 드러나게 (`SearchOrders` > `Tool1`) |
| `description` | **클래스 docstring** | **반드시 채울 것.** "무엇을 하는가 + 언제 호출하는가"를 한두 문장으로. 비면 빈 문자열이 전송돼 모델이 이름만으로 추론해야 함 |
| `input_schema` | `input_schema()`가 반환하는 Pydantic 모델 | 모든 필드에 `Field(description=...)`를 달 것. 타입·required·기본값은 Pydantic이 자동 직렬화 |

> 정적 docstring 대신 입력에 따라 설명을 동적으로 바꾸려면 `description()` 메서드를 오버라이드합니다(기본값이 docstring).
> 최상위 `title`은 제거되고 `$defs`는 인라인 전개된 뒤 제거됩니다(중첩 모델·enum이 `$ref` 없이 그대로 노출 — 예: `status` enum 값들이 스키마에 직접 보임). 위처럼 **필드별 `title`은 남습니다** — 정상입니다.

#### 실행 정책 메서드는 LLM에 전달되지 않는다

`is_concurrency_safe`는 스키마에
**포함되지 않습니다.** 모델 인지용이 아니라 **오케스트레이터가 실행을 제어**하는 런타임 신호입니다:

> `is_concurrency_safe()`가 `True`인 도구만 병렬 배치로 실행됩니다 (읽기 전용 도구가 대표적인 예).
> 외부 상태를 바꾸는(mutating) 도구는 `is_concurrency_safe()`를 `False`로 반환해 순차 실행됩니다.
> 도구 파티셔닝 상세는 [02-tool-orchestration](docs/architecture/02-tool-orchestration.md) 참조.

### ② LLM 백엔드 (LLMProvider)

LLM 교체는 **`LLMProvider`** 하나만 구현하면 됩니다(`friday_agent/api/provider.py`). `config_type`(설정 클래스)을 등록하고 `complete()`가 벤더 응답을 `AssistantResponse`로 **정규화**하면 코어 루프 무변경으로 다른 백엔드가 동작합니다.

```python
from dataclasses import dataclass
from friday_agent.api.provider import (
    LLMProvider, AssistantResponse, StopReason,
    TextBlock, ToolUseBlock, TokenUsage, ContextOverflowError,
)


@dataclass
class MyConfig:                       # 최소 필드 — max_tokens, temperature (LLMConfig 프로토콜)
    max_tokens: int = 16384
    temperature: float | None = None


class MyProvider(LLMProvider[MyConfig]):
    config_type = MyConfig            # ← 필수: 기본 config 생성·검증에 사용

    async def complete(self, messages, system_prompt, tools, config) -> AssistantResponse:
        resp = await call_my_backend(messages, system_prompt, tools, config)   # 벤더 호출
        # 벤더 응답 → AssistantResponse 정규화
        return AssistantResponse(
            content=[TextBlock(text=resp.text)],     # 또는 ToolUseBlock(id, name, input=<dict>)
            stop_reason=StopReason.END_TURN,         # TOOL_USE / MAX_TOKENS / END_TURN
            usage=TokenUsage(input_tokens=resp.in_, output_tokens=resp.out),
        )
        # 컨텍스트 초과면 ContextOverflowError raise → 호출자가 engine.compact(state) 후 재시도
```

구현 규약:

- **`config_type` 설정** + **`complete()` 구현**이 전부의 핵심. 응답은 항상 `AssistantResponse(content, stop_reason, usage, id="", model="")`로 정규화.
- **content 블록**: `TextBlock(text)` · `ToolUseBlock(id, name, input=<이미 파싱된 dict>)` · `ThinkingBlock`(일부 백엔드). `tool_use.input`은 문자열이 아니라 dict여야 함.
- **`stop_reason`**: `END_TURN` / `TOOL_USE` / `MAX_TOKENS` / `CONTEXT_WINDOW_EXCEEDED`. 매핑 없는 벤더 값은 `END_TURN`으로.
- **예외 매핑**: 벤더 예외를 5-클래스 계층(`LLMError`·`RateLimitError`·`ContextOverflowError`·`AuthError`·`TransientError`)으로. 컨텍스트 초과는 `ContextOverflowError`로 **전파**(루프가 잡지 않고 호출자가 `compact`).
- **벤더 규칙은 어댑터 책임**: 빈 `tools`는 API 필드 자체 생략, thinking 활성 시 `temperature` 미전송 등.

벤더 차이표·정규화 세부는 [03-llm-providers](docs/architecture/03-llm-providers.md) 참조.

### ③ 메모리 백엔드 (MemoryStore)

`MemoryStore` 하나가 **영속 백엔드 + 그 도구 표면(`tools()`)을 모두 소유**합니다. `memory=`로 주입한 store가 곧 장착되는 서브시스템 전체입니다(파일 기반 기본 구현은 `FileMemoryStore`).

```python
from friday_agent.memory.store import MemoryStore, MemoryEntry, IndexEntry
from friday_agent.core.engine import FridayAgent


class RedisMemoryStore(MemoryStore):
    async def save(self, entry: MemoryEntry) -> None: ...       # name으로 upsert(같은 name이면 갱신)
    async def read(self, name: str) -> MemoryEntry | None: ...  # 전체 엔트리(body 포함) 또는 None
    async def delete(self, name: str) -> None: ...              # 없으면 무시
    async def load_index(self) -> list[IndexEntry]:             # body 없는 메타데이터만(인덱스 주입용)
        ...
    # tools()는 기본 상속 시 memory_save/read/delete 그대로.
    # 도구 표면을 바꾸려면(예: search 추가) 오버라이드:
    #   def tools(self): return [MemorySave(self), MemoryRead(self), MySearchTool(self)]


engine = FridayAgent(provider=provider, memory=RedisMemoryStore())   # store+도구 통째 대체
```

구현 규약:

- **4개 async 메서드 구현**: `save`(upsert) · `read` · `delete`(없으면 무시) · `load_index`(body 없는 메타만).
- **데이터 모델**: `MemoryEntry(name, description, type: MemoryType, body, updated_at)` · `IndexEntry`(body 없음) · `MemoryType` = `user`/`feedback`/`project`/`reference`.
- **`tools()`** 기본값 = `memory_save`/`memory_read`/`memory_delete` 래퍼. 오버라이드하면 도구 표면이 통째로 바뀝니다.
- 기본 `FileMemoryStore`(→ `FRIDAY_MEMORY.md`)는 단일 프로세스/로컬 편의 — 분산/클라우드 영속은 외부 Storage 기반 store를 주입합니다(`provider`와 동일하게 컨테이너-로컬 재주입, `LoopState`에 미직렬화).

상세는 [08-memory](docs/architecture/08-memory.md) 참조.

---

## 분산 재개 (stateless)

멀티턴을 **턴 단위로 끊어 여러 컨테이너에 분산**하려면 `step()`과 직렬화 API를 씁니다 — 한 턴 = 한 단위, `LoopState` = 컨테이너 경계를 넘는 유일한 상태입니다.

```python
import json
from friday_agent.core.state import LoopState, Terminal
from friday_agent.messages.types import create_user_message

# 컨테이너 A — 첫 턴
state = LoopState(messages=[create_user_message("질문")])
outcome = None
async for item in engine.step(state):           # 한 턴 실행
    if isinstance(item, (LoopState, Terminal)):
        outcome = item
    else:
        persist(item)                           # Message 실시간 처리
if isinstance(outcome, LoopState):
    blob = json.dumps(outcome.to_dict())        # 최종 LoopState sentinel 직렬화해 저장/이송

# 컨테이너 B (다른 시스템) — blob을 로드해 이어서
state = LoopState.from_dict(json.loads(blob))
async for item in engine.step(state):           # 다음 턴 … Terminal까지 반복
    ...
```

루프 상태는 항상 깨끗한 턴 경계에서만 업데이트되므로(`tool_use`↔`tool_result` 정합 보존) `LoopState`를 그대로 직렬화·재개할 수 있습니다. 설계 상세는 [01-core-loop](docs/architecture/01-core-loop.md) 참조.
