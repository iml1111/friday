# 04. 컨텍스트 컴팩션 (Context Compaction)

## ① 목적

컨텍스트 오버플로(토큰 한도 초과) 상황에서 **호출자 주도**로 전체 대화를 요약 메시지 1개로 축소한다.
`step()`은 오버플로를 직접 복구하지 않고 `ContextOverflowError`를 raise해 호출자에게 전파한다.
호출자가 `engine.compact(state)`를 호출하면 LLM을 이용해 대화를 요약하고, 축소된 `LoopState`를 반환한다.
이후 호출자가 `step()`을 재시도함으로써 루프가 복구된다.

---

## ② 담당 파일

| 경로 | 책임 | 핵심 심볼 |
|---|---|---|
| `friday_agent/context/compact.py` | 대화 요약 생성·요약 메시지 구성 | `compact_conversation()`, `create_compact_summary_message()`, `build_compact_prompt()`, `COMPACT_PROMPT`, `MAX_OUTPUT_TOKENS_FOR_SUMMARY` |
| `friday_agent/core/engine.py` | compact 진입점 | `FridayAgent.compact()` |

---

## ③ 핵심 동작 / 흐름

```
step() → ContextOverflowError raise
   └─ 호출자가 engine.compact(state) 호출
         ├─ normalize_for_api(state.messages)          # API 형식 dict 리스트로 변환
         └─ compact_conversation(provider, messages, extra_instructions)
               #   요약기 system = 전용 SUMMARIZER_SYSTEM_PROMPT (호출자 role 미전달)
               ├─ build_compact_prompt(extra_instructions)를 마지막 user 메시지로 추가
               ├─ provider.complete(tools=[], config=config_type(max_tokens=20000))
               │     # tools=[] : 요약 중 도구 호출 엄격 금지
               └─ 응답 텍스트에서 <summary>...</summary> 추출
                     · <analysis> 블록은 폐기
                     · <summary> 태그가 없으면 전체 텍스트를 폴백으로 사용
         → create_compact_summary_message(summary)     # is_compact_summary=True 인 user 메시지
         → LoopState(messages=[summary], turn_count 보존) 반환
   └─ 호출자가 step() 재시도
```

### COMPACT_PROMPT 보존 항목

`context/compact.py:37`의 `_COMPACT_PROMPT_HEAD`는 LLM에게 다음 9개 항목을 요약 안에 보존하도록 지시한다:

1. Primary Request and Intent (주요 요청·의도)
2. Key Technical Concepts (핵심 기술 개념)
3. Files and Code Sections (실제 코드 스니펫 포함)
4. Errors and Fixes (오류 및 수정 내역)
5. Problem Solving Progress (문제 해결 진행 상황)
6. All User Messages (도구 결과 제외)
7. Pending Tasks (미완료 작업)
8. Current Work (최근 작업의 정확한 설명)
9. Optional Next Step (직접 인용 포함)

출력 형식은 `<analysis>스크래치패드</analysis><summary>요약 본문</summary>`이며, `<analysis>` 블록은 추출 시 폐기된다. `COMPACT_PROMPT`는 9개 항목 외에 체계적 분석 지침과 포맷 트레일러를 포함해 `<summary>` 포맷 준수율을 높인다(원본 `services/compact/prompt.ts` 기준, 개발 특화 표현은 일반화).

**no-tools 가드는 1회만 등장한다** (첫 `CRITICAL:` 줄). `compact_conversation`이 `tools=[]`로 호출하고 두 어댑터 모두 빈 리스트면 `tools` 필드를 통째로 생략하므로(`anthropic_provider.py:145-146`, `openai_provider.py:142-143`) 모델은 애초에 `tool_use` 블록을 만들 수 없다. 남긴 1회는 `tools` 인자를 무시하는 서드파티 `LLMProvider` 구현에 대한 belt-and-suspenders다 — 반복은 사문(死文)이라 걷어냈다. 회귀 방지는 `tests/test_compact.py::test_no_tools_guard_appears_exactly_once`.

### 도메인 지침 주입 슬롯 (opt-in)

요약 호출은 호출자의 `system_prompt`를 받지 않는다(전용 `SUMMARIZER_SYSTEM_PROMPT` 사용). 따라서 "이 도메인에서는 요약에 무엇을 반드시 남겨야 하는가"를 지정할 **유일한 통로**가 `FridayAgent(..., compact_instructions="...")`다. 값은 `engine.compact()` → `compact_conversation(extra_instructions=...)` → `build_compact_prompt()`로 전달된다.

```
<head: CRITICAL 가드(no-tools 1회·no-questions) + 분석 지침 + 9개 섹션 명세>

Domain-specific requirements for this summary
(these take precedence over the generic sections above):
<compact_instructions 본문>                              ← 주입 슬롯

<tail: Output format + REMINDER(출력 포맷 계약)>
```

- **위치가 계약이다** — 슬롯은 9번 섹션 뒤·`Output format` 앞이다. 트레일링 `REMINDER`가 `<analysis>`/`<summary>` **출력 포맷 계약**을 지고 있고 recency가 그 준수를 만든다. 태그 없는 응답은 전체 텍스트 폴백으로 떨어져 스크래치패드가 요약에 섞여 들어가므로, 주입 블록이 이 자리를 밀어내면 안 된다.
- **precedence 헤더** — "take precedence over the generic sections above" 문구 덕에 섹션 추가(10번을 더 써라)와 기존 섹션 재정의(3번은 이렇게 다뤄라)가 모두 한 슬롯으로 커버된다. 그래서 프롬프트 **전체 교체 옵션은 두지 않는다** — 교체를 허용하면 vendored 사본이 업스트림 프롬프트 개선을 못 받는 포크 상태가 된다.
- **기본값은 무변경** — `compact_instructions=""`(기본)이면 `build_compact_prompt()`의 출력이 `COMPACT_PROMPT`와 **바이트 단위로 동일**하다. 공백만 든 문자열도 no-op.
- 엔진 로컬 설정이라 `LoopState` 직렬화 면은 불변 — 분산 재개 시 `provider`·`system_prompt`와 함께 컨테이너-로컬로 재주입한다.

---

## ④ 공개 API

### `engine.compact(state) -> LoopState`

```python
async def compact(self, state: LoopState) -> LoopState:
```

- 인자: 현재 `LoopState` (messages 포함)
- 반환: 요약 메시지 1개(`messages=[summary_message]`)와 보존된 `turn_count`를 담은 새 `LoopState`
- 호출자는 반환된 축소 상태를 `step()`에 그대로 넘겨 재시도한다
- 도메인 요약 지침은 호출 인자가 아니라 생성자 `FridayAgent(..., compact_instructions=...)`로 한 번 설정한다

### `compact_conversation()` — 직접 사용 시

```python
async def compact_conversation(
    *,
    provider: LLMProvider,
    messages: list[dict],
    extra_instructions: str = "",
) -> str:
```

- `tools=[]`로 고정. `config_type(max_tokens=20000)` 사용.
- 반환값: 추출된 요약 텍스트 문자열 (태그 제거 후)

### `build_compact_prompt()` — 프롬프트 렌더링

```python
def build_compact_prompt(extra_instructions: str = "") -> str:
```

- 인자가 비면(또는 공백뿐이면) `COMPACT_PROMPT`와 동일한 문자열을 반환한다.
- `COMPACT_PROMPT` 상수 자체가 `build_compact_prompt()`의 결과로 정의돼 있어 둘이 갈라질 수 없다.

---

## ⑤ 의존성

| 의존 모듈 | 사용 심볼 | 용도 |
|---|---|---|
| `friday_agent/api/provider.py` | `LLMProvider.complete()`, `LLMProvider.config_type` | 요약 LLM 호출 |
| `friday_agent/messages/types.py` | `create_user_message()` | 요약 메시지 생성 |
| `friday_agent/messages/normalize.py` | `normalize_for_api()` | engine 측 호출 전 변환 |

호출 흐름 전체 맥락은 [01-core-loop](01-core-loop.md) 참조.

---

## ⑥ 유지보수 주의점

- **`tools=[]` 필수**: `compact_conversation()` 내 `provider.complete()` 호출은 반드시 `tools=[]`여야 한다(`context/compact.py:159`). **이것이 실제 강제 수단이다** — 프롬프트의 no-tools 문구는 서드파티 프로바이더용 보조일 뿐이고, 도구 호출 차단은 이 한 줄이 한다. 요약 중 도구 호출이 허용되면 tool_use↔tool_result 쌍 정합성이 깨질 수 있다.
- **`is_compact_summary=True` 플래그**: `create_compact_summary_message()`가 생성하는 user 메시지는 `is_compact_summary=True`로 표시된다(`context/compact.py:122`). 이 플래그는 [05-messages](05-messages.md)의 메시지 유형 분류와 연동되며, 제거하거나 누락하면 메시지 필터링 로직이 요약 메시지를 일반 사용자 메시지로 오분류할 수 있다.
- **`turn_count` 보존**: `engine.compact()`는 `LoopState(messages=[summary_message], turn_count=state.turn_count)`로 반환한다(`core/engine.py:163`). 축소 후 `turn_count`가 리셋되지 않아야 관측 지표가 유지된다.
- **`<summary>` 태그 폴백**: `<summary>` 태그가 없으면 전체 응답 텍스트를 그대로 사용한다(`context/compact.py:173-174`). LLM이 포맷을 어기더라도 루프가 중단되지 않도록 설계된 방어 코드다. 포맷 준수율이 낮아지면 요약 품질 저하로 이어지므로 프롬프트 수정 시 주의한다.
- **요약기 system 분리**: 요약 호출의 system은 전용 `SUMMARIZER_SYSTEM_PROMPT`(`context/compact.py`)다. `engine.compact()`는 호출자 도메인 role을 요약 호출에 넘기지 않는다(요약은 "요약" 작업이 본질).
- **재개(continuation) 프레이밍**: `create_compact_summary_message()`는 요약을 "이전 대화 이어받기" 프리앰블 + "곧장 재개, 추가 질문 금지" 지시로 감싼다. 컴팩션 후 재개를 매끄럽게 하기 위한 것으로, 본문 변경 시 재개 동작에 영향을 준다.

---

## ⑦ 설계 근거 (Why)

기존 가이드라인(04)의 **내부 Auto/Reactive Compact** 방식(루프 내부에서 오버플로를 감지·복구)과 달리, 본 구현은 **호출자 주도(caller-owned)** 방식을 채택한다:

- `step()`은 오버플로 시 `ContextOverflowError`를 raise해 즉시 호출자에게 전파한다.
- 호출자가 `engine.compact(state)`를 명시적으로 호출해 복구한 뒤 `step()`을 재시도한다.

이 분리를 통해:
1. **라이브러리 경계 명확화**: `step()`은 단일 턴 실행만 담당하며, 복구 정책은 호출자가 결정한다.
2. **분산 재개 친화성**: `ContextOverflowError`는 직렬화 가능하므로 분산 오케스트레이터가 상태를 저장·복구하기 쉽다.
3. **테스트 용이성**: compact 경로를 호출자 측에서 독립적으로 테스트할 수 있다.
