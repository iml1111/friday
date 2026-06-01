# 02 — 도구 오케스트레이션

> **관련 문서**: [00-overview](00-overview.md) · [01-core-loop](01-core-loop.md) · [06-invariants](06-invariants.md)

---

## ① 목적

`tool_use` 블록 목록을 받아 **파티셔닝 → 병렬/순차 실행 → 블록 원래 순서로 결과 반환**하는 계층이다.

핵심 계약:
1. `tool_use`↔`tool_result` 쌍 정합성을 항상 보존한다 — 병렬 실행·에러 경로 어디서도 무너지지 않는다.
2. 결과는 `asyncio.gather`의 인자 순서 보장을 활용해 **블록 입력 순서대로** yield된다.
3. 알 수 없는 도구·예외는 배치 전체를 중단하지 않고, 해당 블록에 에러 `tool_result`를 생성한다.

---

## ② 담당 파일

| 경로 | 책임 | 핵심 심볼 |
|---|---|---|
| `friday_agent/tools/orchestrator.py` | 파티셔닝 · 병렬/순차 실행 · 순서 보존 | `partition_tool_calls()`, `run_tools()`, `Batch` |
| `friday_agent/tools/base.py` | Tool 인터페이스 · 결과 타입 | `Tool`, `ToolResult` |
| `friday_agent/tools/builtin/example_tool.py` | 데모 도구 (작성 패턴) | `ExampleTool` |

---

## ③ 핵심 동작 / 데이터 흐름

### 파티셔닝 (`partition_tool_calls`)

`orchestrator.py:68` — `blocks: list[ContentBlock]`를 받아 `list[Batch]`를 반환한다.

```
[RO, RO, RO, MUT, RO, RO]
  ↓
[Batch(is_concurrency_safe=True, 3블록),
 Batch(is_concurrency_safe=False, 1블록),
 Batch(is_concurrency_safe=True, 2블록)]
```

**병합 규칙**: 연속된 concurrency-safe 블록은 하나의 병렬 배치로 합쳐진다. non-safe 블록은 항상 독립 배치가 된다.

**보수적 fallback** (`orchestrator.py:41–65`): 다음 중 하나라도 해당하면 non-safe로 처리한다.
- 도구를 찾을 수 없음 (unknown tool)
- 입력이 `None`
- Pydantic 스키마 검증 실패
- `is_concurrency_safe()` 자체에서 예외 발생

**concurrency-safe 판정**: 병렬 배치 여부는 **`is_concurrency_safe()` 하나만으로 결정된다**. `_is_concurrency_safe()` (`orchestrator.py:41–65`)는 스키마 검증 통과 후 `tool.is_concurrency_safe()`만 호출한다(`orchestrator.py:63`). 즉 `is_concurrency_safe() → True`만 반환하면 병렬 대상이 된다. 기본값은 `False`이므로 명시적 오버라이드 없이는 순차 실행된다.

---

### 실행 경로 — `run_tools`

`orchestrator.py:145` / `core/loop.py:40,216` — `run_one_turn()`이 직접 호출하는 유일한 실행 경로다.

```python
# core/loop.py
from friday_agent.tools.orchestrator import run_tools

# core/loop.py — effects_sink로 각 도구의 state_effect 수집 (메시지 yield 순서는 불변)
async for result_msg in run_tools(tool_use_blocks, tools, max_concurrency=max_concurrency, effects_sink=effects):
```

동작:
1. `partition_tool_calls(blocks, tools)`로 배치 목록을 만든다.
2. 병렬 배치(`is_concurrency_safe=True` AND `len > 1`): `asyncio.Semaphore(max_concurrency)`로 동시성을 제한하고 `asyncio.gather`로 실행. 결과는 블록 순서 그대로 yield.
3. 그 외(순차 배치 또는 블록이 1개): 블록을 하나씩 순서대로 실행.
4. 알 수 없는 도구·예외 → 에러 `tool_result` 생성, 배치 계속 진행.
5. `effects_sink`(선택 인자)가 주어지면, 각 도구의 `ToolResult.state_effect`(None 아님)를 **블록 순서대로** sink에 누적한다. 메시지 스트림은 불변이며, `_run_single_tool`은 `(Message, state_effect)` 튜플을 반환한다. 루프가 이 sink를 모아 다음 `LoopState.todos`를 계산한다(상태 단독 writer = 루프).

---

### 데이터 흐름 요약

```
run_one_turn()
    │
    ├─ tool_use 블록 추출
    │
    └─ run_tools(blocks, tools, max_concurrency)  ← 루프 연결 경로
            │
            ├─ partition_tool_calls()
            │       └─ [Batch(safe, N), Batch(non-safe, 1), ...]
            │
            ├─ 병렬 Batch: asyncio.gather + Semaphore → 블록 순서로 yield
            └─ 순차 Batch: 블록 하나씩 순서대로 yield
                    │
                    각 블록 → _run_single_tool() → tool_result Message
                                  └─ 에러 시: 에러 tool_result (배치 비중단)
```

---

## ④ 공개 API / 확장 포인트

### 커스텀 도구 작성 (BYO Tool)

`Tool`을 상속하고 `input_schema()`와 `call()`을 구현하는 것이 전부다.

```python
from pydantic import BaseModel, Field
from friday_agent.tools.base import Tool, ToolResult


class WeatherInput(BaseModel):
    city: str = Field(description="날씨를 조회할 도시 이름")


class WeatherTool(Tool):
    """지정한 도시의 현재 날씨를 반환합니다."""

    name = "get_weather"

    def input_schema(self) -> type[BaseModel]:
        return WeatherInput

    def is_concurrency_safe(self, input: dict) -> bool:
        return True  # 병렬 실행 허용

    async def call(self, args: dict) -> ToolResult:
        parsed = WeatherInput(**args)
        # 실제 외부 API 호출로 교체
        return ToolResult(data=f"{parsed.city}: 맑음, 22°C")
```

병렬 배치 대상이 되려면 `is_concurrency_safe()`가 `True`를 반환하면 된다 — 파티셔닝(`_is_concurrency_safe`, `orchestrator.py:41–65`)이 이 술어 하나만 참조한다.

---

### `Tool` 메서드 — 보수적 기본값

`tools/base.py:51`

| 메서드 | 반환 타입 | 기본값 | 설명 |
|---|---|---|---|
| `input_schema()` | `type[BaseModel]` | _(추상)_ | 입력 스키마. **필수 구현** |
| `call(args)` | `ToolResult` | _(추상)_ | 실행 로직. **필수 구현** |
| `is_concurrency_safe(input)` | `bool` | `False` | 병렬 실행 허용 여부 |

---

### `ToolResult`

`tools/base.py:20`

```python
ToolResult(
    data,                   # 실행 결과 (문자열 또는 구조화 데이터)
    is_error=False,
    state_effect=None,      # 선언적 상태 변이(예 {"todos": [...]}); 루프가 단독 적용
)
```

---

### `get_tool_schema()`

`tools/base.py:124` — Pydantic v2 스키마에서 `title`과 `$defs`를 제거하고 API에 전달할 형태로 반환한다.

```python
{
    "name": self.name,
    "description": "...",
    "input_schema": { ... }  # title, $defs 제거됨
}
```

---

## ⑤ 의존성

| 방향 | 모듈 | 이유 |
|---|---|---|
| 사용함 | `messages/types.py` | `ContentBlock`, `create_tool_result_message` |
| 사용함 | `pydantic` | 입력 스키마 검증 (`model_validate`, `model_json_schema`) |
| 호출됨 | `core/loop.py` | `run_tools`를 import·호출 (`loop.py:40,216`) |

---

## ⑥ 유지보수 주의점

**결과 순서 불변식** — `run_tools`는 병렬 실행하더라도 결과를 **tool_use 블록 입력 순서대로** 반환한다 ([06-invariants](06-invariants.md) 참조). `asyncio.gather`가 인자 순서를 보장하므로 재정렬 코드 추가는 금지다.

**미완 tool_use 백필** — 에러 경로에서 일부 tool_use 블록에 대응하는 tool_result가 누락될 수 있다. 이 경우의 백필은 **`core/loop.py`의 `yield_missing_tool_result_blocks()`가 담당**한다. 오케스트레이터는 이 책임을 지지 않는다.


**`is_concurrency_safe` 보수적 기본값** — 새 도구를 작성하면 기본값이 `False`이므로 의도치 않게 병렬 배치가 형성되지 않는다. 병렬 실행을 원하면 `is_concurrency_safe()`를 명시적으로 `True`로 오버라이드해야 한다 — 파티셔닝은 이 술어 하나만 참조한다.

---

## ⑦ 설계 근거 (Why)

**파티셔닝 전략** — read-only 도구만 병렬로 묶고, 변경 도구는 격리하는 방식은 Friday 원본 설계에서 검증된 안전한 기본값이다. 알 수 없는 도구나 스키마 검증 실패를 non-safe로 처리하는 보수적 fallback은 새 도구가 배포되는 중간 상태에서 병렬 실행으로 인한 부작용을 방지한다.
