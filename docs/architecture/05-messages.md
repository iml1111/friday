# 05. 메시지 표현 (Messages)

## ① 목적

내부 대화 표현(`Message` / `ContentBlock`)과 LLM API wire 형식 간 변환을 담당한다.
루프·오케스트레이터·컴팩터가 공통으로 사용하는 메시지 생성 헬퍼와, API 전송 직전의 필터·정규화 함수를 제공한다.

---

## ② 담당 파일

| 경로 | 책임 | 핵심 심볼 |
|---|---|---|
| `friday_agent/messages/types.py` | 내부 Message·ContentBlock·생성 헬퍼 + 턴-로컬 리마인더 프로토콜 | `Message`, `ContentBlock`, `create_user_message()`, `create_tool_result_message()`, `wrap_system_reminder()`, `SYSTEM_REMINDER_PREFIX` |
| `friday_agent/messages/normalize.py` | API 형식 변환 | `normalize_for_api()` |

---

## ③ 핵심 동작 / 타입

### `ContentBlock` — `messages/types.py:8`

단일 flat dataclass로, 모든 블록 종류를 하나의 타입으로 표현한다.

| 필드 | 타입 | 적용 블록 | 설명 |
|---|---|---|---|
| `type` | `str` | 전체 | `"text"` \| `"tool_use"` \| `"tool_result"` \| `"thinking"` |
| `text` | `str \| None` | `text`, `thinking` | 텍스트 내용 / thinking 내용 |
| `id` | `str \| None` | `tool_use` | 도구 호출 ID |
| `name` | `str \| None` | `tool_use` | 도구 이름 |
| `input` | `dict \| None` | `tool_use` | 도구 인자 (Anthropic SDK가 dict로 파싱해서 넘김) |
| `tool_use_id` | `str \| None` | `tool_result` | 대응하는 tool_use의 ID |
| `content` | `str \| None` | `tool_result` | 도구 실행 결과 텍스트 |
| `is_error` | `bool` | `tool_result` | 실행 오류 여부 (기본값 `False`) |

> **⚠️ 이름 충돌 주의**: `messages/types.py`의 `ContentBlock`(단일 flat dataclass, 위 정의)과 `api/provider.py:34`의 `ContentBlock`(`Union[TextBlock, ToolUseBlock, ThinkingBlock]` 앨리어스)은 **이름만 같고 완전히 별개의 타입**이다. 전자는 내부 표현용 dataclass이고, 후자는 Anthropic SDK 타입들의 Union 앨리어스다. 두 파일을 같은 스코프에서 임포트할 때 충돌에 주의한다.

---

### `Message` — `messages/types.py:48`

루프가 `state.messages` 리스트로 관리하는 내부 대화 단위다.

| 필드 | 타입 | 설명 |
|---|---|---|
| `uuid` | `str` | 자동 생성 UUID |
| `type` | `str` | 내부 분류: `"user"` \| `"assistant"` \| `"system"` |
| `role` | `str` | API wire role: `"user"` \| `"assistant"` |
| `content` | `list[ContentBlock]` | 블록 목록 |
| `is_api_error_message` | `bool` | 합성 에러 메시지 (루프가 복구 분기에 사용) |
| `is_compact_summary` | `bool` | 컴팩션이 생성한 요약 메시지 |
| `is_meta` | `bool` | 시스템 내부 전용 메시지 (API 전송 제외) |

---

### 생성 헬퍼

#### `create_user_message()` — `messages/types.py:81`

```python
def create_user_message(
    content: str | list[ContentBlock],
    *,
    is_meta: bool = False,
    is_compact_summary: bool = False,
) -> Message:
```

- `content`가 `str`이면 `ContentBlock(type="text", text=content)` 한 개로 자동 래핑한다.
- `type="user"`, `role="user"`로 고정.

#### `create_tool_result_message()` — `messages/types.py:98`

```python
def create_tool_result_message(
    tool_use_id: str,
    result_text: str,
    is_error: bool = False,
) -> Message:
```

- `type="user"`, `role="user"`로 고정 (API role 교대 규칙 준수).
- `is_error=True`이면 `content`를 `<tool_use_error>result_text</tool_use_error>`로 래핑한다.
- `is_error` 값은 `ContentBlock.is_error`에 그대로 전달된다.

---

### `normalize_for_api()` — `messages/normalize.py:6`

```python
def normalize_for_api(messages: list[Message]) -> list[dict]:
```

내부 `Message` 리스트를 LLM API가 수신하는 `{"role": str, "content": list[dict]}` 형식으로 변환한다.

**3단계 필터** (순서대로 적용):

1. `is_meta=True` 메시지 제외 — 루프 내부 전용 합성 메시지는 API에 보내지 않는다.
2. `role`이 비어 있는 메시지 제외 — 시스템 내부 메시지는 wire role이 없다.
3. `content`가 비어 있는 메시지 제외 — API는 빈 content를 거부한다.

**블록 변환 규칙** (`_convert_content_blocks()`):

| 블록 타입 | 출력 필드 |
|---|---|
| `text` | `{"type":"text", "text":...}` (`text`가 `None`이면 생략) |
| `tool_use` | `{"type":"tool_use", "id":..., "name":..., "input":...}` |
| `tool_result` | `{"type":"tool_result", "tool_use_id":..., "content":...}` (is_error=True이면 `"is_error":True` 추가) |
| `thinking` | `{"type":"thinking", "thinking":...}` (`text` 필드를 `thinking` 키로 에코) |

> **thinking 블록 verbatim 에코**: thinking 블록은 생략 없이 그대로 API에 되돌려야 한다. 중간 thinking 블록 하나라도 누락되면 API가 해당 턴의 대화 구조를 거부한다. 이 규칙은 [06-invariants](06-invariants.md)에서도 별도로 명시한다.

---

## ④ 공개 API

루프(`core/loop.py`), 오케스트레이터(`tools/orchestrator.py`), 컴팩터(`context/compact.py`)가 앞의 3개 심볼을 직접 임포트해 사용하고, 턴-로컬 리마인더 프로토콜 2개 심볼은 리마인더 생산자(`core/loop.py`·`memory/store.py`)와 감지자(`api/anthropic_provider.py`)가 공유한다.

| 심볼 | 위치 | 역할 |
|---|---|---|
| `create_user_message()` | `messages/types.py:81` | 사용자 입력·메타 메시지 생성 |
| `create_tool_result_message()` | `messages/types.py:98` | 도구 결과 메시지 생성 |
| `normalize_for_api()` | `messages/normalize.py:6` | API 전송 직전 변환 |
| `wrap_system_reminder()` | `messages/types.py:124` | 턴-로컬 리마인더 `<system-reminder>` 래핑 (모든 생산자 공용) |
| `SYSTEM_REMINDER_PREFIX` | `messages/types.py:121` | Anthropic 어댑터 캐시 breakpoint 스킵의 감지 계약 |

---

## ⑤ 의존성

`messages/` 패키지는 외부 의존성이 없다(순수 표준 dataclass + 타입 변환).
역으로 아래 서브시스템이 이 패키지에 의존한다:

| 의존 모듈 | 사용 심볼 |
|---|---|
| `friday_agent/core/loop.py` | `normalize_for_api()`, `create_tool_result_message()` |
| `friday_agent/core/engine.py` | `normalize_for_api()` |
| `friday_agent/tools/orchestrator.py` | `create_tool_result_message()` |
| `friday_agent/context/compact.py` | `create_user_message()` (`create_compact_summary_message()` 내부) |

> `create_user_message()`는 `context/compact.py`에서만 사용된다.

---

## ⑥ 유지보수 주의점

- **role 교대 규칙**: `tool_result` 메시지는 반드시 `role="user"`여야 하고, 대화의 첫 메시지도 user여야 한다. `create_tool_result_message()`가 이를 강제한다. 위반 시 API가 요청을 거부한다. 상세 규칙은 [06-invariants](06-invariants.md) 참조.
- **thinking verbatim 에코**: `normalize_for_api()`의 `thinking` 블록 변환은 절대 생략하거나 변형해서는 안 된다. 누락 시 API 턴이 깨진다([06-invariants](06-invariants.md)).
- **tool_result `<tool_use_error>` 래핑**: `create_tool_result_message(is_error=True)`는 `content` 텍스트를 `<tool_use_error>...</tool_use_error>`로 래핑한다(`messages/types.py:109`). LLM이 에러 컨텍스트를 인식하도록 하는 규약이므로 임의로 태그를 바꾸지 않는다.
- **`<system-reminder>` 태그 단일 정의**: 턴-로컬 리마인더 태그는 `messages/types.py`의 `wrap_system_reminder()`/`SYSTEM_REMINDER_PREFIX` 한 곳에서만 정의된다. 생산자(todo·메모리 인덱스)와 Anthropic 어댑터의 breakpoint 스킵 감지가 이 상수를 공유하므로 리터럴을 재복제하면 캐시 스킵이 조용히 깨진다.
- **`is_meta` 플래그 남용 금지**: `is_meta=True` 메시지는 `normalize_for_api()`에서 투명하게 제거된다. 루프 내부 합성 메시지 외에 사용하면 API에 전달해야 할 메시지가 누락될 수 있다.
- **이름 충돌**: `messages/types.py`의 `ContentBlock`과 `api/provider.py`의 `ContentBlock`(Union 앨리어스)을 같은 파일에서 임포트하면 이름이 충돌한다. 필요 시 `as` 앨리어스로 구분한다.

---

## ⑦ 설계 근거 (Why)

**단일 flat dataclass 선택**: `ContentBlock`을 블록 종류별 별도 클래스 대신 단일 flat dataclass로 설계한 이유는 루프·오케스트레이터가 블록 종류를 `type` 문자열 하나로 분기할 수 있어 패턴 매칭이 단순해지기 때문이다. 필드 대부분이 `None`이 되는 트레이드오프를 수용한 의도적 선택이다.

**`normalize_for_api()` 분리**: 내부 표현과 API wire 형식을 분리함으로써 LLM 벤더가 바뀌어도 `Message`/`ContentBlock` 타입은 유지하고 변환 레이어만 교체할 수 있다. `thinking` 블록의 verbatim 에코 같은 벤더 전용 규칙도 변환 레이어에 국소화된다.
