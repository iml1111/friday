# 설계 제안서: TodoWrite 도구·가이던스 항상-주입 (빌트인화)

- **작성일**: 2026-06-01
- **대상**: `friday_agent` (분산 에이전트 루프 SDK)
- **상태**: 설계 승인됨 · 미구현
- **선행**: [`2026-06-01-todo-tracking-design.md`](2026-06-01-todo-tracking-design.md) — todo 추적 메커니즘(LoopState.todos · state_effect · per-turn 리마인더)을 구현 완료한 상태를 전제로 한다. 본 문서는 그 기능을 **호출자 주입 없이 항상 켜지는 빌트인**으로 승격한다.

---

## 1. 배경 & 동기

todo 추적은 이미 구현됐지만, 현재는 **호출자가 직접 배선**해야만 동작한다:

- `TodoWrite` 도구를 `FridayAgent(tools=[...])`에 직접 넣어야 모델이 호출할 수 있다.
- 언제·어떻게 `TodoWrite`를 쓰는지(개념·프로토콜)에 대한 가이던스를 호출자가 자기 `system_prompt`에 손수 써넣어야 한다 (현재 `scripts/run_agent.py`가 그렇게 하고 있다).

즉 호출자가 둘 다 빠뜨리면 기능이 사실상 죽는다. 반면 friday에는 이미 **항상-켜짐(no opt-out) 주입의 선례**가 있다 — `assemble_system_prompt()`가 `GENERAL_AGENT_GUIDANCE`를 호출자 프롬프트 뒤에 무조건 덧붙인다. todo 추적도 동일한 위상으로 끌어올려, **호출자가 아무것도 하지 않아도 무조건 들어가게** 한다.

---

## 2. 목표 / 비목표

### 목표
- `TodoWrite` 도구를 `FridayAgent`가 **항상 자동 등록** — 호출자 주입 불필요.
- todo 사용 **가이던스(개념)**를 시스템 프롬프트에 **항상 자동 주입** — `GENERAL_AGENT_GUIDANCE`와 동일한 무조건 위상.
- 빌트인 `TodoWrite`의 `state_effect` 계약(루프가 의존)을 **정합성 보장** — 호출자가 같은 이름으로 가릴 수 없게 한다.

### 비목표 (YAGNI)
- opt-out 플래그 없음. `FridayAgent.__init__` 시그니처 불변.
- per-turn 리마인더(`render_todo_reminder`)는 **이미 항상-켜짐** — 변경 없음.
- 빌트인 도구를 범용 플러그인 레지스트리로 일반화하지 않는다. 현재 빌트인은 `TodoWrite` 하나지만, 접근자(`builtin_tools()`) 형태로 두어 추가 여지만 남긴다.
- 호출자 todos의 입력 경계(예: `run_agent.py`의 user 입력 간 유지)·사람용 표시는 **호출자 관심사**로 남긴다(SDK는 per-step만 책임).

---

## 3. 핵심 설계 결정

| # | 결정 | 근거 |
|---|---|---|
| D1 | **엔진 경계 주입.** `FridayAgent.__init__`에서 빌트인 도구를 병합, `assemble_system_prompt()`에서 가이던스를 덧붙임. | 조립 지점 단일화. 코어 루프(`run_one_turn`)는 특정 도구 이름에 결합되지 않은 채 유지 — 선언적 `state_effect` 설계의 탈결합을 보존. |
| D2 | **이름 충돌 = 에러(fail-fast).** 호출자가 빌트인과 같은 이름의 도구를 넘기면 `ValueError`. | Anthropic은 중복 도구 이름을 거부하므로 dedupe는 어차피 필수. "조용히 호출자 우선"은 호출자가 깨진 `TodoWrite` 변종으로 루프의 todos 계약을 가릴 위험이 있어, **명시적 거부**로 정합성을 지킨다. |
| D3 | **가이던스는 코어(prompts.py)에 위치.** `TODO_GUIDANCE` 상수를 `api/prompts.py`에 두고 `assemble_system_prompt`가 덧붙임. | 리마인더 렌더링이 `core/loop.py`에 사는 것과 동일 철학 — todo 도구는 상태 없는 순수 함수로 두고, todo-인지 텍스트는 코어가 소유. `compact()`는 이 경로를 안 거치므로 가이던스가 요약에 새지 않음(GENERAL 블록과 동일). |

---

## 4. 변경 사항

### 4.1 도구 자동 등록 — `friday_agent/tools/builtin/__init__.py`

현재 비어 있는 패키지 `__init__`에 빌트인 접근자를 둔다:

```python
from friday_agent.tools.base import Tool
from friday_agent.tools.builtin.todo_write import TodoWrite


def builtin_tools() -> list[Tool]:
    """Tools FridayAgent always registers, even without caller injection."""
    return [TodoWrite()]
```

### 4.2 병합 + 충돌 검증 — `friday_agent/core/engine.py`

`FridayAgent.__init__`에서 (기존 config 타입 검증 뒤에) 호출자 도구와 빌트인을 병합하되, 이름 충돌은 거부한다:

```python
from friday_agent.tools.builtin import builtin_tools
...
caller_tools = tools if tools is not None else []
builtins = builtin_tools()
builtin_names = {t.name for t in builtins}
clash = sorted({t.name for t in caller_tools if t.name in builtin_names})
if clash:
    raise ValueError(
        f"FridayAgent: {clash} are built-in tools that are always registered; "
        f"remove them from the tools list."
    )
self._tools = [*caller_tools, *builtins]
```

`step()`은 이미 `self._tools`에서 `tool_schemas`를 만들므로 스키마는 자동 반영된다(추가 변경 없음).

### 4.3 가이던스 자동 주입 — `friday_agent/api/prompts.py`

`GENERAL_AGENT_GUIDANCE` 옆에 `TODO_GUIDANCE` 상수를 추가하고, `assemble_system_prompt`가 빈 블록을 걸러 이어붙인다:

```python
TODO_GUIDANCE = """# Task tracking
You have a TodoWrite tool for tracking multi-step work. For any task with several
steps, call TodoWrite first to lay out the plan, then keep it updated as you go.
 - Always send the COMPLETE list each call; it replaces the previous one.
 - Keep exactly one item in_progress at a time; mark items completed the moment they are done.
 - Skip it for trivial single-step tasks.
The current list is surfaced to you each turn inside a <system-reminder>; it reflects
tracked state, not necessarily the user's latest instruction."""


def assemble_system_prompt(system_prompt: str) -> SystemPrompt:
    blocks = [b for b in (system_prompt, GENERAL_AGENT_GUIDANCE, TODO_GUIDANCE) if b]
    return SystemPrompt(text="\n\n".join(blocks))
```

### 4.4 데이터 흐름

```
FridayAgent.__init__(tools=호출자_도구)
        └─ 이름 충돌 검증 → self._tools = 호출자_도구 + [TodoWrite()]

FridayAgent.step(state)
        └─ tool_schemas = [t.get_tool_schema() for t in self._tools]   ← TodoWrite 항상 포함
        └─ run_one_turn(...)
               └─ assemble_system_prompt(base) = base + GENERAL_AGENT_GUIDANCE + TODO_GUIDANCE
               └─ if state.todos: <system-reminder> 주입   (변경 없음)
```

---

## 5. 하위 정리

### 5.1 `scripts/run_agent.py` (필수)
빌트인화로 인해 명시적 배선이 **중복이자 에러 유발**이 된다:
- `tools=[ExampleTool(), TodoWrite()]` → `tools=[ExampleTool()]` (TodoWrite 자동). 안 고치면 D2 검증에서 `ValueError`.
- `TodoWrite` import 제거.
- `_SYSTEM_PROMPT`의 손수 쓴 todo 문장 제거(가이던스 자동 주입).
- 배너 문구를 빌트인 반영으로 갱신.
- **유지**: user 입력 경계 간 `todos` 유지, `_render_todos` 표시(호출자 관심사).

### 5.2 문서 (SSOT 동기화)
- `docs/architecture/01-core-loop.md` §⑥ — 항상-주입 노트에 "TodoWrite 도구 + 가이던스 자동 등록" 추가.
- `docs/architecture/02-tool-orchestration.md` — 빌트인 자동 등록(이름 충돌 시 `ValueError`) 노트.
- `CLAUDE.md` — 공개 API/도구 설명 줄에 빌트인 todo 항상-주입을 한 줄 반영.

---

## 6. 테스트 (키 불필요 · fake provider)

- `builtin_tools()`가 `TodoWrite` 인스턴스를 반환.
- `FridayAgent(provider, tools=[])` → `self._tools`와 `step()`의 `tool_schemas`에 `TodoWrite` 포함.
- `FridayAgent(provider, tools=[ExampleTool()])` → ExampleTool + TodoWrite 둘 다 등록.
- `FridayAgent(provider, tools=[TodoWrite()])` → `ValueError`(충돌 거부). 메시지에 도구 이름 포함.
- `assemble_system_prompt("")`와 `assemble_system_prompt("base")` 모두 `TODO_GUIDANCE` 텍스트 포함, 그리고 `GENERAL_AGENT_GUIDANCE`도 포함.
- 통합: 호출자 주입 도구 없이 fake provider가 `TodoWrite` tool_use를 방출 → 호출이 정상 처리되고 다음 `LoopState.todos`가 갱신.
- 회귀: 기존 142 테스트 그린 유지(특히 `compact()` 경로엔 `TODO_GUIDANCE` 미포함 확인).

---

## 7. 정합성·범위 점검

- **차터 부합**: "시스템 프롬프트 조립 machinery"는 포함 범위이며, 본 변경은 기존 `GENERAL_AGENT_GUIDANCE` 항상-주입 메커니즘을 그대로 미러링한다. 제외 항목(서브에이전트 위임·스트리밍·모델 fallback 등)을 추가하지 않는다.
- **루프 불변식 보존**: 코어 루프는 도구 이름을 모른 채 유지(주입은 엔진 경계). `tool_use↔tool_result` 정합성과 단독 state writer 불변식 불변.
- **분산 재개 불변**: 도구·config는 직렬화 대상이 아니므로 컨테이너마다 동일 코드로 재구성 → 빌트인 주입은 결정론적. `LoopState` serde 변화 없음.
