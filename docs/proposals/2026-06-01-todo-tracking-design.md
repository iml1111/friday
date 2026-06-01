# 설계 제안서: Todo / Task 추적 접목

- **작성일**: 2026-06-01
- **대상**: `friday_agent` (분산 에이전트 루프 SDK)
- **상태**: 설계 승인됨 · 미구현 · 현행 코드(`LoopState` sentinel) 기준 정정됨 (본 문서는 제안서이며 구현 계획·코드는 포함하지 않음)
- **출처**: Claude Code의 `todo_reminder`(per-turn 자동 재주입) 메커니즘을 friday의 순수-도구·분산 구조에 맞춰 이식

---

## 1. 배경 & 동기

Claude Code는 멀티스텝 작업에서 **todo 목록을 매 턴 컨텍스트에 자동 재주입**(`todo_reminder` attachment)해, 모델이 도구를 다시 호출하지 않아도 항상 현재 진행상황을 인지한다. 긴 대화에서 작업 목록이 attention에서 밀려나는 것을 막는 anti-drift 장치다.

friday는 현재 이 개념이 **전혀 없다**. 확인된 구조적 사실:

- 턴 루프(`core/loop.py` › `run_one_turn`)에는 system prompt 외에 컨텍스트를 끼워넣을 지점이 없다. 유일한 choke point는 `messages_for_query = list(state_messages)` 한 곳이다.
- `is_meta=True` 메시지는 `messages/normalize.py` › `normalize_for_api`가 **API 호출에서 제거**한다. 즉 friday의 `is_meta`는 "transcript에 남기되 모델에겐 숨김" — Claude Code의 system-reminder("모델에겐 보이되 실제 유저 입력은 아님")와 **정반대**다.
- 도구는 `call(args) -> ToolResult`로 **순수 함수**이며 루프 상태에 접근하지 못한다(`tools/orchestrator.py`가 `tool.call(block.input)`만 전달). 동시성-안전 도구는 `asyncio.gather`로 **병렬** 실행되므로 공유 상태 직접 변이는 레이스 위험이 있다.

따라서 "살아있는 todo 리마인더"를 넣으려면 **모델에게 보이되 `state`에는 쌓이지 않는 새 주입 경로**와, **순수 도구가 상태를 갱신하는 경로**를 함께 설계해야 한다. 이것이 본 작업의 알맹이다.

---

## 2. 목표 / 비목표

### 목표
- 멀티스텝 작업의 todo 목록을 모델이 도구 재호출 없이 매 턴 인지(자동 리마인더, Claude Code 충실 재현).
- todo 상태가 `LoopState` JSON 직렬화에 포함되어 **턴 단위 분산 재개** 시에도 동일하게 보존.
- friday의 **순수-도구 설계와 단독 state writer(루프) 불변식**을 깨지 않음.

### 비목표 (YAGNI)
- 범용 "컨텍스트 프로바이더" 채널로 일반화하지 않는다. todo에 한정하되, 상태 변이 effect 프로토콜만 확장 가능하게 열어둔다.
- CLAUDE.md / 메모리 / RULES의 per-turn 재주입은 범위 밖. 이는 `system_prompt` 입력으로 호환되는 영역으로 본다. (단 §8의 설계 노트 참조: friday의 `system_prompt`는 세션 1회 정적이라 Claude Code의 매 턴 재주입과는 미세 차이가 있다.)
- "todo 없음" 넛지·빈 상태 리마인더는 넣지 않는다(목록이 비면 무주입).
- 별도 `TodoRead` 도구는 두지 않는다(리마인더가 항상 현재 목록을 노출하므로 불필요).

---

## 3. 핵심 설계 결정 (요약)

| # | 결정 | 근거 |
|---|---|---|
| D1 | **자동 리마인더** 방식 (도구 전용 X) | Claude Code `todo_reminder` 충실 재현; 긴 대화 anti-drift |
| D2 | todo를 **`LoopState.todos` 명시 필드**에 저장 | single source of truth; `LoopState` 직렬화로 분산 재개 보존 |
| D3 | 순수 도구는 **`ToolResult.state_effect` 선언적 반환**, 루프가 적용 | 루프가 단독 state writer 유지(레이스 0, 결정론); 루프-도구 이름 결합 0; BYO-tool 확장 철학 부합 |
| D4 | 리마인더는 **API view 사본(`api_input_messages`)**의 **끝 user 턴에 합류**시켜 주입 | API엔 보이고 `state`/`LoopState`엔 안 쌓임; role 교대 보존 |

---

## 4. 설계 상세

### 4.1 데이터 모델 & 상태 — `core/state.py`

`LoopState`에 JSON-native 필드 하나를 추가한다. 각 todo는 `{"content": str, "status": "pending"|"in_progress"|"completed"}` dict이며, 입력 검증은 TodoWrite의 Pydantic 스키마(§4.3)가 경계에서 담당하므로 상태는 평범한 dict로 보관해 직렬화를 단순하게 유지한다.

```python
@dataclass
class LoopState:
    messages: list[Any]
    turn_count: int = 1
    todos: list[dict] = field(default_factory=list)          # NEW

    def to_dict(self):
        return {"messages": [m.to_dict() for m in self.messages],
                "turn_count": self.turn_count,
                "todos": self.todos}                          # NEW (이미 JSON-native)

    @classmethod
    def from_dict(cls, d):
        return cls(messages=[Message.from_dict(m) for m in d["messages"]],
                   turn_count=d.get("turn_count", 1),
                   todos=d.get("todos", []))                  # NEW, 하위호환 기본값
```

`LoopState.to_dict/from_dict`가 todos를 직접 운반하므로 **분산 재개 시 todos가 그대로 이송**된다 — `LoopState` 자체가 "계속" sentinel 겸 이송 단위다. `d.get("todos", [])`로 todos가 없는 옛 상태 dict도 깨지지 않는다(하위호환).

### 4.2 변이 프로토콜 — `ToolResult.state_effect` (`tools/base.py`)

순수 도구가 직접 mutate하는 대신 **선언적 effect를 반환**하고, 루프가 그것을 적용한다. 루프는 여전히 유일한 state writer다.

```python
@dataclass
class ToolResult:
    data: Any
    is_error: bool = False
    state_effect: dict | None = None     # NEW: 예) {"todos": [...]}
```

루프 측 적용기(작고 도구-이름에 무지하다 — `'todos'`라는 **상태 개념**만 알고 `'TodoWrite'`라는 **도구 이름**은 모른다):

```python
def apply_state_effects(todos: list[dict], effects: list[dict]) -> list[dict]:
    for eff in effects:
        if "todos" in eff:
            todos = eff["todos"]
    return todos
```

향후 다른 stateful effect 키도 이 적용기에서 확장한다. 루프-도구 결합이 0이라 통합자가 루프 수술 없이 자신만의 stateful 도구를 작성할 수 있다.

### 4.3 TodoWrite 도구 — `tools/builtin/todo_write.py` (신규)

전체 리스트 교체 방식(Claude Code와 동일 — 멱등하고 분산 친화적). docstring이 곧 LLM에 전달되는 description이므로 "무엇을·언제" 충실히 작성한다.

```python
class TodoItem(BaseModel):
    content: str = Field(description="Imperative task description, e.g. 'Add the login endpoint'")
    status: Literal["pending", "in_progress", "completed"] = Field(
        description="pending = not started, in_progress = active now, completed = done")

class TodoWriteInput(BaseModel):
    todos: list[TodoItem] = Field(
        description="The COMPLETE updated list; it replaces the previous list entirely")

class TodoWrite(Tool):
    """Create and manage a structured task list for the current session. Use for
    multi-step work (3+ steps) to track progress and show the user a plan. Send the
    ENTIRE updated list every call — it replaces the previous one. Keep exactly one
    item in_progress at a time; mark items completed the moment they are done."""
    name = "TodoWrite"

    def input_schema(self):
        return TodoWriteInput

    def is_concurrency_safe(self, input_data: dict) -> bool:
        return False                                  # 상태 변이 → 순차 실행

    async def call(self, args: dict) -> ToolResult:
        todos = [t.model_dump() for t in TodoWriteInput(**args).todos]
        return ToolResult(data=f"Updated todo list ({len(todos)} items).",
                          state_effect={"todos": todos})
```

- `is_concurrency_safe=False`로 다른 도구와 병렬 배치되지 않는다(추가 안전망). 실제로는 effect를 루프가 적용하므로 race는 원천적으로 없다.
- "exactly one in_progress" 규칙은 **soft**로 둔다(description 안내 + 필요 시 `data`에 경고 문자열). 하드 거부는 하지 않는다 — 턴을 깨지 않기 위해.

### 4.4 리마인더 주입 채널 — `core/loop.py`

choke point는 `state_messages`를 API view로 복사하는 지점(`list(state_messages)`) 직후, `normalize_for_api` 직전. **끝 user 턴을 system-reminder 블록이 추가된 새 Message로 재구성**해 API view 전용 사본(`api_input_messages`)에만 주입하고, **다음 LoopState 조립은 리마인더가 없는 `state_messages`를 쓴다**(§4.5의 누수 방지). 공유 Message 객체를 mutate하지 않으므로 `state.messages`/`LoopState`는 불변이고, API엔 보이며, role 교대도 보존된다(새 consecutive user 메시지를 만들지 않고 끝 user 턴에 블록만 합류).

```python
api_input_messages = list(state_messages)                # API view 전용 (다음 LoopState는 state_messages에서 조립)
if state.todos:                                          # 비어 있으면 무주입(노이즈 0)
    api_input_messages = with_todo_reminder(api_input_messages, state.todos)
api_messages = normalize_for_api(api_input_messages)     # is_meta 아님 → API로 전달됨
```

```python
def with_todo_reminder(messages, todos):
    block = ContentBlock(type="text", text=render_todo_reminder(todos))
    if messages and messages[-1].role == "user":
        last = messages[-1]
        merged = replace(last, content=[*last.content, block])   # 새 객체(끝 user 턴에 합류)
        return [*messages[:-1], merged]
    return [*messages, create_user_message([block])]             # 방어적(턴 시작엔 도달 안 함)
```

렌더 형식:

```
<system-reminder>
Current todo list (update via TodoWrite as you progress; keep one item in_progress):
- [in_progress] Wire state_effect into the loop
- [pending] Add reminder injection
- [completed] Extend LoopState.todos
This reflects tracked state, not necessarily the user's latest instruction.
</system-reminder>
```

**시점 의미**: 리마인더는 턴 시작 시점의 `state.todos`(= 커밋된 상태)를 반영한다. 모델이 이번 턴에 TodoWrite를 호출하면 그 effect는 **다음 턴** state에 적용되어 다음 리마인더에 나타난다(이번 턴엔 `ToolResult.data`로 즉시 확인). 이는 Claude Code와 동일한 의미론이다.

### 4.5 effect 수집 & 다음 LoopState 구성 — `tools/orchestrator.py` + `core/loop.py`

도구의 `tool.call`은 `run_tools`가 직접 부르지 않고 **`_run_single_tool` 내부**에서 실행된다. 현재 `_run_single_tool`은 `ToolResult`로 tool_result 메시지를 만든 뒤 결과 객체(따라서 `state_effect`)를 버린다. 그래서 effect를 끌어내려면 `_run_single_tool`이 **메시지와 effect를 함께 반환**하고, `run_tools`가 그것을 호출자 sink에 블록 순서대로 누적한다(메시지 스트림과 effect 수집을 분리; tool_result yield는 기존 그대로).

```python
# orchestrator._run_single_tool(block, tools) -> tuple[Message, dict | None]
result = await tool.call(block.input or {})
msg = create_tool_result_message(tool_use_id=block.id or "", result_text=str(result.data), is_error=result.is_error)
return msg, result.state_effect          # 알 수 없는 도구/예외 경로는 (error_msg, None)

# orchestrator.run_tools(..., effects_sink: list[dict] | None = None)
#   각 배치에서 (msg, effect)를 받아 msg는 yield, effect는 None이 아니면 sink에 append (블록 순서 보존)
#   기존 호출자는 effects_sink 생략 → 동작 불변
```

루프는 sink를 모아 다음 state의 todos를 계산한다. **다음 `LoopState`는 리마인더가 없는 `state_messages`에서 조립**한다(§6 비영속 불변식 보존 — `api_input_messages`가 아니다):

```python
effects: list[dict] = []
async for result_msg in run_tools(tool_use_blocks, tools, max_concurrency=..., effects_sink=effects):
    tool_results.append(result_msg)
    yield result_msg

next_todos = apply_state_effects(state.todos, effects)   # effect 없으면 그대로 carry-forward
yield LoopState(
    messages=[*state_messages, *assistant_messages, *tool_results],   # 깨끗한 원본 — 리마인더 미포함
    turn_count=next_turn_count,
    todos=next_todos)                                    # NEW
```

effect가 없는 턴은 `state.todos`가 변경 없이 다음 턴으로 carry-forward된다.

---

## 5. 한 턴 데이터 흐름

```
state(todos=[A:pending])
  → api_input_messages = state.messages + <reminder: A pending>     (API엔 보임, state엔 X)
  → provider.complete(...)
  → assistant: tool_use TodoWrite(todos=[A:in_progress, B:pending])
  → run_tools:
        ToolResult(data="Updated (2)", state_effect={todos:[A:in_progress, B:pending]})
        · tool_result 메시지 yield (모델에 즉시 확인)
        · effects_sink += that effect
  → LoopState(todos = [A:in_progress, B:pending])   ← 다음 턴 리마인더가 이걸 반영
```

---

## 6. 불변식 보존 & 분산 안전

- **tool_use ↔ tool_result 정합**: 리마인더는 기존 user 턴에 합류(둘 사이에 끼지 않음), effect 적용은 메시지 구조를 바꾸지 않음 → 정합 보존.
- **role 교대**: 새 consecutive user 메시지를 만들지 않고 끝 user 턴에 블록만 추가 → API 안전.
- **분산 재개**: todos는 `LoopState`로 직렬화되고, 리마인더는 비영속·턴마다 todos에서 파생 → 컨테이너 B에서 재개해도 동일 리마인더가 재생성됨(결정론적).
- **레이스 0**: 도구는 상태를 만지지 않고 effect만 반환, 루프가 단독 writer → 병렬 배치와 무관.
- **하위호환·점진 도입**: `from_dict`의 기본 `todos=[]`로 옛 체크포인트 OK. TodoWrite를 `tools`에 등록하지 않으면 todos는 빈 채로 남아 리마인더·effect 훅이 전부 무해한 no-op이 됨.

---

## 7. 테스트 전략

- **단위**
  - `LoopState` 직렬화 왕복: todos 포함, 그리고 todos 키가 없는 옛 dict 로드.
  - `TodoWrite.call`: 올바른 `state_effect` 반환, 스키마 검증, `is_concurrency_safe=False`.
  - `apply_state_effects`: effect 있음 → 교체, effect 없음 → carry-forward.
  - `render_todo_reminder`: 비어 있으면 무주입, 항목 있으면 system-reminder 렌더.
  - `with_todo_reminder`: 끝 user 턴에 합류, `state.messages` 불변, consecutive user 미발생; 다음 `LoopState.messages`에 리마인더 미포함(비영속).
- **통합 (fake provider)**
  - 모델이 TodoWrite 호출 → 다음 턴 `api_messages`에 갱신된 리마인더 포함, `LoopState` 직렬화에 todos 존재.
- **분산**
  - TodoWrite 턴 후 `LoopState`를 `to_dict → json → from_dict`("컨테이너 B") → todos 보존 → 리마인더 동일 재생성.

---

## 8. 변경 표면 (touch map)

| 파일 | 변경 | 성격 |
|---|---|---|
| `core/state.py` | `LoopState.todos` 필드 + `to_dict`/`from_dict` | 상태 계약 확장 |
| `tools/base.py` | `ToolResult.state_effect` 필드 | 변이 프로토콜 |
| `tools/orchestrator.py` | `_run_single_tool`이 `(Message, state_effect)` 반환 + `run_tools(effects_sink=)`로 누적 | 배선 |
| `core/loop.py` | ① `render_todo_reminder`/`with_todo_reminder`로 리마인더 주입(API view 전용 `api_input_messages`) ② `apply_state_effects`로 다음 `LoopState` 구성(깨끗한 `state_messages`에서) | 루프가 todos 상태 read/apply 단독 소유 |
| `core/engine.py` | `compact()`가 `todos=state.todos` carry-forward | 1줄 |
| `tools/builtin/todo_write.py` | **신규** TodoWrite 도구 (상태 effect 선언만; 렌더는 core 소유) | 신규 |
| `tools/builtin/__init__.py` | 변경 없음 — 기존 패턴대로 모듈 경로로 직접 import(`ExampleTool`과 동일) | — |
| `tests/` | 단위 + 통합(분산 재개) | 검증 |
| `docs/architecture/` | 01-core-loop(리마인더), 02-tool-orchestration(state_effect), 07-data-models(todos) 반영 | 문서 동기화 |

> `core/engine.py`는 **`compact()` 1줄만 변경** — 요약 후 새 `LoopState`를 조립할 때 `todos=state.todos`를 carry-forward한다. 컴팩션은 긴 대화에서 발생하는데 그때가 todo anti-drift가 가장 필요한 시점이므로 todos를 drop하면 안 된다(요약은 prose, todos는 구조화 상태라 별도 보존; `turn_count`와 동일 취급). 리마인더·effect 적용 자체는 전부 `run_one_turn` 내부에서 `state.todos`로 처리된다.

> **설계 노트**: friday의 `system_prompt`는 세션 1회 정적 조립(`api/prompts.py` › `assemble_system_prompt`)이라, Claude Code가 환경 컨텍스트를 매 턴 재주입하는 것과는 미세한 충실도 차이가 있다. 본 제안의 리마인더 채널(§4.4)은 todo에 한정해 그 "매 턴 재주입" 패턴을 처음으로 도입하며, 동일 메커니즘은 추후 다른 per-turn 컨텍스트로 확장 가능한 형태로 남겨둔다(범위 밖).
