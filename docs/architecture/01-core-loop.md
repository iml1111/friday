# 01. 코어 루프 (Core Loop)

에이전트 루프의 **단일 턴 실행**과 **종료 / 복구 / 재개**를 담당하는 서브시스템이다.

---

## ① 목적

`core/` 서브시스템은 두 가지 책임을 가진다.

1. **단일 턴 실행** — LLM 호출 → 응답 수신 → tool_use 실행 → 결과 피드백의 한 사이클을 완주한다 (`run_one_turn()`).
2. **종료 / 복구 / 재개** — 모든 경로(정상 완료·오류·컨텍스트 오버플로)에서 `tool_use↔tool_result` 정합성을 보존하고, 턴 경계마다 직렬화 가능한 `LoopState`를 방출해 stateless 분산 재개를 지원한다.

while-true 드라이버는 존재하지 않는다. 호출자가 `step()`을 반복 호출하며 루프를 직접 구동한다.

---

## ② 담당 파일

| 경로 | 책임 | 핵심 심볼 |
|---|---|---|
| `friday_agent/core/loop.py` | 단일 턴 실행·stop_reason 분기·백필 | `run_one_turn()`, `yield_missing_tool_result_blocks()` |
| `friday_agent/core/engine.py` | 외부 진입점, provider 직접 주입, 메모리 도구 등록·섹션 주입 | `FridayAgent.step()`, `FridayAgent.compact()` |
| `friday_agent/core/state.py` | 루프 상태·종료 타입 + JSON serde | `Terminal`, `LoopState` (`to_dict`/`from_dict`) |

---

## ③ 핵심 동작 — `run_one_turn()` 턴 라이프사이클

`run_one_turn()`은 `friday_agent/core/loop.py:167`에 정의된 `AsyncGenerator`다. 한 턴 동안 생성된 모든 `Message`를 yield한 뒤, 마지막에 정확히 **sentinel 1개** (`Terminal` 또는 `LoopState`)를 yield하고 종료한다.

### 실행 순서

```
1. api_input_messages = list(state.messages)
      └─ with_turn_reminders()로 <system-reminder> 주입 (API view 전용·비영속):
         [todo 리마인더(state.todos 있을 때)] + turn_reminders 파라미터(엔진이 메모리 인덱스 전달) 순
      └─ normalize_for_api(api_input_messages) → provider.complete()   ← LLM 호출

2. 응답 → _to_assistant_message()       ← 내부 Message로 변환 후 yield

3. tool_use 블록이 있으면
      └─ run_tools(effects_sink=effects) ← 도구 병렬 실행 + state_effect 수집
            └─ tool_result message yield (각 결과)

4. 턴 끝: sentinel 1개 yield (next_todos = apply_state_effects(state.todos, effects))
      LoopState              ─ 루프 계속 (깨끗한 state.messages + next_todos)
      Terminal               ─ 루프 종료
```

### 종료 / 전이 분기표

`run_one_turn()`이 실제로 방출하는 사유는 2가지다.

| 조건 | 결과 |
|---|---|
| tool_use 없음 (end_turn 등 비-tool stop) | `Terminal(reason="completed")` |
| `LLMError` (오버플로 제외) | `Terminal(reason="model_error", error=...)` + 백필 |
| `ContextOverflowError` | **호출자에게 raise** (Terminal 아님) — `engine.compact()` 후 재시도 |
| 도구 실행 완료 | `LoopState(..., turn_count+1)` |

### 백필 (`yield_missing_tool_result_blocks`)

`LLMError`로 턴이 중단되면, 아직 결과를 받지 못한 tool_use 블록에 대해 `friday_agent/core/loop.py:76` › `yield_missing_tool_result_blocks()`가 합성 오류 `tool_result`를 생성해 정합성을 복구한다. 상세는 [06-invariants](06-invariants.md) 참조.

---

## ④ 공개 API / 확장 포인트

### `FridayAgent` 생성

```python
FridayAgent(
    provider,              # LLMProvider — 필수
    tools=None,            # list[Tool]
    system_prompt="",
    config=None,           # 미지정 시 provider.config_type() 기본값
    max_concurrency=10,
    memory=None,           # MemoryStore — 미지정 시 메모리 서브시스템 미장착(opt-in); store가 곧 도구 표면
    compact_instructions="",  # compact() 요약 프롬프트에 끼울 도메인 요구사항; 빈 문자열이면 프롬프트 무변경
)
```

`config`가 `provider.config_type`과 타입이 다르면 즉시 `ValueError`를 발생시킨다.

컨텍스트 주입 면은 의도적으로 단순하다: 정적 콘텐츠는 호출자가 `system_prompt` 문자열 하나로 넘긴다(여러 섹션은 호출자 쪽에서 `"\n\n".join(...)`으로 조합). 동적(턴별 가변) 콘텐츠 주입 표면은 엔진에 없다 — SDK 내부(todo·메모리 인덱스)만 turn-local 리마인더 기계(`run_one_turn`의 `turn_reminders`)를 사용한다.

`system_prompt`는 **턴 루프 전용**이다 — `compact()`의 요약 호출은 전용 `SUMMARIZER_SYSTEM_PROMPT`로 돌기 때문에 이 프롬프트가 닿지 않는다. 요약이 무엇을 보존해야 하는지는 별도 문자열 `compact_instructions`로 지정한다 (상세: [04-context-compaction](04-context-compaction.md#도메인-지침-주입-슬롯-opt-in)).

### `engine.step(state) -> AsyncGenerator[Message | LoopState | Terminal, None]`

한 턴을 실행하는 **비동기 제너레이터**다. 턴이 진행되는 동안 생성된 각 `Message`(어시스턴트 응답, 각 tool_result)를 즉시 yield하고, 마지막에 정확히 **sentinel 1개**(`LoopState` 또는 `Terminal`)를 yield한 뒤 종료한다.

```python
async for item in engine.step(state):
    if isinstance(item, (LoopState, Terminal)):
        outcome = item        # LoopState → 다음 턴 (outcome을 그대로 state로)
                              # Terminal  → 루프 종료
    else:
        render(item)          # Message: 어시스턴트 응답 또는 tool_result — 도착 즉시 소비 가능
```

`ContextOverflowError`는 소비하지 않고 그대로 호출자에게 전파된다.

### `await engine.compact(state) -> LoopState`

`state.messages` 전체를 요약 1개로 축소한다. `turn_count`는 보존된다. `ContextOverflowError` 복구 흐름:

```
step(state) → ContextOverflowError 발생
    └─ compact(state) → 축소된 LoopState
          └─ step(축소된 state) 재시도
```

상세는 [04-context-compaction](04-context-compaction.md) 참조.

### 상태 타입

| 타입 | 정의 위치 | 역할 |
|---|---|---|
| `LoopState(messages, turn_count=1, todos=[])` | `core/state.py:34` | 직렬화 가능한 루프 이송 단위 + 턴 경계 "계속" 재개 sentinel |
| `Terminal(reason, error=None)` | `core/state.py:19` | 루프 종료 sentinel |

---

## ⑤ 의존성

| 의존 모듈 | 사용 목적 |
|---|---|
| `friday_agent/messages/normalize.py` | `normalize_for_api()` — 내부 Message → API 페이로드 변환 |
| `friday_agent/tools/orchestrator.py` | `run_tools()` — 도구 병렬 실행 |
| `friday_agent/api/provider.py` | `LLMProvider`, `LLMError`, `ContextOverflowError`, 응답 블록 타입 |
| `friday_agent/context/compact.py` | `compact_conversation()`, `create_compact_summary_message()` — `engine.compact()` 구현체 |
| `friday_agent/api/prompts.py` | `assemble_system_prompt()` — 시스템 프롬프트 조립 + 범용 행동블록 주입 |
| `friday_agent/memory/store.py` | `MEMORY_INSTRUCTIONS`(정적 지침, system용) + `build_memory_reminder()`(턴별 인덱스 리마인더); 기본 `FileMemoryStore`/`MemoryStore` 타입 |

---

## ⑥ 유지보수 주의점

- **컨텍스트 윈도우 관리는 호출자 책임.** `step()`은 `state.messages`를 그대로 API에 전송한다. 토큰 예산을 초과하면 `ContextOverflowError`를 던지므로, 호출자가 `engine.compact(state)`로 축소 후 재시도해야 한다.
- **범용 행동블록 자동 주입.** `run_one_turn()`은 `GENERAL_AGENT_GUIDANCE`(프롬프트 인젝션 플래깅·`<system-reminder>` 의미·행동의 가역성·간결성 등)를 호출자 `system_prompt` **앞에 항상** 주입해 전송한다 (`assemble_system_prompt()`). 순서는 범용→구체 — 도메인 프롬프트가 마지막에 와서 그 규칙이 recency 우위로 범용 지침을 오버라이드한다. opt-out 플래그는 없다. 컴팩션 요약 호출(`engine.compact()`)은 이 경로를 거치지 않으므로 범용블록이 요약에 섞이지 않는다.
- **TodoWrite 도구·가이던스 자동 주입(빌트인).** `FridayAgent`는 `builtin_tools()`(`tools/builtin/__init__.py`)의 도구를 호출자 도구에 항상 병합하고, `assemble_system_prompt()`가 `TODO_GUIDANCE`를 항상 덧붙인다(opt-out 없음). 호출자가 빌트인과 같은 이름의 도구를 주입하면 `FridayAgent.__init__`이 `ValueError`로 거부한다. 컴팩션 요약은 이 프롬프트 경로를 거치지 않으므로 `TODO_GUIDANCE`가 요약에 섞이지 않는다.
- **메모리 프롬프트 주입(opt-in, 정적/동적 분리).** `memory=` store가 장착된 경우에만: `engine.step()`이 정적 지침 `MEMORY_INSTRUCTIONS`는 base 시스템 프롬프트 앞에(범용→구체, 세션 내 byte-stable), 라이브 인덱스는 매 턴 `build_memory_reminder(self._memory)`로 렌더해 turn-local 리마인더(`turn_reminders` 경로)로 `messages[-1]`에만 싣는다. `memory=None`(기본)이면 이 경로 전체가 생략된다. `compact()`는 이 경로를 거치지 않아 메모리 인덱스가 요약에 새지 않는다. `MemoryStore`는 `LoopState`에 직렬화되지 않으므로(컨테이너-로컬 재주입) 분산 재개 serde는 불변이다. 프롬프트 캐싱(always-on) 관점: `memory_save`/`delete`가 인덱스를 바꿔도 system 프리픽스·대화 히스토리 캐시는 살아남는다 — 변하는 것은 breakpoint 밖 리마인더 블록뿐(인덱스를 system에 두면 저장 1회가 대화 전체 캐시를 무효화한다). 상세는 [08-memory](08-memory.md) 참조.
- **`tool_use↔tool_result` 쌍 보존.** `LLMError` 경로에서 백필(`yield_missing_tool_result_blocks`)이 작동해 LLM API 거부를 방지한다. 이 불변식이 깨지면 다음 API 호출이 즉시 실패한다. 상세는 [06-invariants](06-invariants.md) 참조.
- **루프 상태는 깨끗한 턴 경계에서만 업데이트.** `LoopState`는 도구 결과가 모두 수집된 뒤에만 yield되므로, 직렬화·재개 시 중간 상태가 누락되지 않는다.
- **serde는 provider·config를 직렬화하지 않음.** `LoopState.to_dict()` / `LoopState.from_dict()`는 messages + turn_count + todos만 왕복한다. provider·config는 컨테이너 로컬 객체로 간주해 재개 시 다시 주입한다.
- **턴별 리마인더는 비영속.** `run_one_turn()`이 매 턴 `with_turn_reminders()`로 `<system-reminder>` 블록들(todo 리마인더 + 호출자 제공 `turn_reminders`)을 **API view 전용 사본(`api_input_messages`)**의 끝 user 턴에 합류시켜 전송한다. 다음 `LoopState`는 리마인더가 없는 `state_messages`에서 조립되므로 리마인더는 상태에 쌓이지 않고, 분산 재개 시 `todos` 등 원천에서 결정론적으로 재생성된다. `engine.compact()`도 `todos`를 carry-forward한다(요약은 prose, todos는 구조화 상태). 캐시 불변식: 턴별 가변 텍스트는 전부 `messages[-1]`에만 실린다 — `messages[-2]` 이전이 byte-stable해야 프로바이더의 롤링 breakpoint가 계속 적중한다.

---

## ⑦ 설계 근거(Why)

**왜 단일 턴 step-only인가?**  
라이브러리 내부에 while-true 드라이버를 두지 않음으로써 분산 큐·서버리스·서버 핸들러 등 다양한 서버 사이드 실행 컨텍스트에서 동일한 `FridayAgent.step()`을 재사용할 수 있다.

**왜 `LoopState`를 그대로 방출해 stateless 재개하나?**  
턴 경계에서 직렬화 가능한 `LoopState`를 방출하면 프로세스 재시작이나 컨테이너 이동 후에도 `step()`에 같은 `LoopState`를 넘겨 재개할 수 있다. 타입의 `to_dict()`/`from_dict()` 메서드가 JSON 왕복을 담당하며, provider·config는 직렬화 대상에서 제외한다(컨테이너 로컬).
