# 07. 데이터 모델 카탈로그 (Data Models)

`friday_agent`의 모든 데이터 모델을 한눈에 보는 참조 문서다. 각 모델의 **필드 요약 · 직렬화 여부 · 정의 위치**만 담고, 상세 동작·변환 규칙은 해당 서브시스템 문서로 링크한다.

> 이 문서는 카탈로그다. 깊은 설명(변환 흐름·불변식·설계 근거)은 중복하지 않고 [01](01-core-loop.md)·[02](02-tool-orchestration.md)·[03](03-llm-providers.md)·[05](05-messages.md)로 위임한다.

---

## ① 직렬화 경계 (가장 중요)

`to_dict()` / `from_dict()`를 가진 모델만 **분산 재개의 이송 단위**다. 나머지는 컨테이너 로컬 런타임 객체로 간주하며 재개 시 다시 주입한다.

| 모델 | 정의 | serde | 이송 역할 |
|---|---|---|---|
| `Message` | `messages/types.py` | ✅ | 대화 히스토리 단위 |
| `ContentBlock` (내부 flat) | `messages/types.py` | ✅ | Message 내부 블록 |
| `LoopState` | `core/state.py` | ✅ | 루프 상태(messages + turn_count) |
| `Checkpoint` | `core/state.py` | ✅ | 턴 경계 재개 sentinel — 이송 단위는 `json.dumps(checkpoint.to_dict())` |
| 그 외 전부 | — | ❌ | 런타임 전용 (provider·config·응답·도구 결과 등) |

---

## ② 계층별 모델

### 2.1 대화 메시지 — `messages/types.py` → 상세 [05-messages](05-messages.md)

#### `Message` — 루프가 `state.messages`로 관리하는 대화 단위

| 필드 | 타입 | 의미 |
|---|---|---|
| `uuid` | `str` | 자동 생성 UUID |
| `type` | `str` | 내부 분류: `user` \| `assistant` \| `system` |
| `role` | `str` | API wire role: `user` \| `assistant` |
| `content` | `list[ContentBlock]` | 블록 목록 |
| `is_api_error_message` | `bool` | 합성 에러 메시지 (복구 분기에 사용) |
| `is_compact_summary` | `bool` | 컴팩션이 생성한 요약 |
| `is_meta` | `bool` | 시스템 내부 전용 (API 전송 제외) |

`type`(의미)과 `role`(와이어)은 별개다 — `tool_result`도 `role="user"`로 나간다.

#### `ContentBlock` (내부 flat) — 모든 블록 종류를 단일 dataclass로 표현

| 필드 | 적용 `type` | 의미 |
|---|---|---|
| `type` | 전체 | `text` \| `tool_use` \| `tool_result` \| `thinking` |
| `text` | `text`·`thinking` | 텍스트 / thinking 내용 |
| `id`·`name`·`input` | `tool_use` | 호출 ID·도구명·인자(dict, SDK가 파싱) |
| `tool_use_id`·`content`·`is_error` | `tool_result` | 대응 tool_use ID·결과 텍스트·오류 여부 |

> **⚠️ 이름 충돌**: 이 `ContentBlock`(내부 flat dataclass)과 `api/provider.py`의 `ContentBlock`(아래 2.3, Union 앨리어스)은 **이름만 같고 별개 타입**이다. 같은 스코프에서 import 시 `as`로 구분한다.

**생성 헬퍼**: `create_user_message()`, `create_tool_result_message()` (`is_error=True`면 `<tool_use_error>…</tool_use_error>`로 래핑). 상세는 [05](05-messages.md) 참조.

---

### 2.2 루프 상태 / 제어 — `core/state.py` → 상세 [01-core-loop](01-core-loop.md)

#### `LoopState` — 직렬화 가능한 루프 상태

| 필드 | 타입 | 의미 |
|---|---|---|
| `messages` | `list[Message]` | 전체 히스토리 |
| `turn_count` | `int` (기본 1) | 턴 카운터 |

provider·config 등 비직렬화 런타임 객체는 의도적으로 제외한다.

#### `Checkpoint` — "계속" sentinel

| 필드 | 타입 | 의미 |
|---|---|---|
| `state` | `LoopState` | 다음 턴 상태 |

`run_one_turn()`이 턴 완료 후 루프가 계속될 때 yield한다 (Terminal과 대비).

#### `Terminal` — "종료" sentinel

| 필드 | 타입 | 의미 |
|---|---|---|
| `reason` | `str` | 종료 사유 |
| `error` | `Exception \| None` | 오류 객체 (model_error 시) |

> **⚠️ reason drift**: `run_one_turn()`이 **실제로 emit하는 reason은 `completed`·`model_error` 2가지**다 ([01-core-loop](01-core-loop.md) 분기표 기준). `state.py`의 docstring은 `blocking_limit`·`image_error`·`hook_stopped`도 나열하지만 현재 실행 경로에서는 방출되지 않는다. 컨텍스트 오버플로는 Terminal이 아니라 `ContextOverflowError`로 호출자에게 raise된다.

---

### 2.3 LLM 응답 (와이어) — `api/provider.py` → 상세 [03-llm-providers](03-llm-providers.md)

`provider.complete()`가 벤더 응답을 정규화한 결과. `loop._to_assistant_message()`가 이를 내부 `Message`/`ContentBlock`으로 변환한다.

#### `AssistantResponse`

| 필드 | 타입 | 의미 |
|---|---|---|
| `content` | `list[ContentBlock]` (아래 union) | 응답 블록 목록 |
| `stop_reason` | `StopReason` | 정규화된 종료 신호 |
| `usage` | `TokenUsage` | 토큰 카운터 |
| `id` | `str` | 응답 ID (기본 `""`) |
| `model` | `str` | 모델명 (기본 `""`) |

#### 응답 블록 union — `ContentBlock = TextBlock | ToolUseBlock | ThinkingBlock`

| 블록 | 필드 |
|---|---|
| `TextBlock` | `type="text"`, `text` |
| `ToolUseBlock` | `type="tool_use"`, `id`, `name`, `input`(dict) |
| `ThinkingBlock` | `type="thinking"`, `thinking`, `signature` (일부 백엔드만) |

#### `StopReason` (str Enum)

| 멤버 | 값 |
|---|---|
| `END_TURN` | `"end_turn"` |
| `TOOL_USE` | `"tool_use"` |
| `MAX_TOKENS` | `"max_tokens"` |
| `CONTEXT_WINDOW_EXCEEDED` | `"model_context_window_exceeded"` |

#### `TokenUsage`

`input_tokens` · `output_tokens` · `cache_creation_input_tokens` · `cache_read_input_tokens` (전부 `int`, 캐시 미지원 백엔드는 0).

#### 예외 계층 (모델은 아니지만 흐름 제어 타입)

`LLMError`(base) → `RateLimitError` · `ContextOverflowError` · `AuthError` · `TransientError`. **`ContextOverflowError`가 호출자 주도 compact를 트리거**한다 ([04-context-compaction](04-context-compaction.md)).

---

### 2.4 호출 설정 — `api/provider.py` · `api/configs.py` → 상세 [03-llm-providers](03-llm-providers.md)

공유 베이스 클래스 없음 (벤더별 완전 분리, 미정의 필드는 생성 시 `TypeError`).

| 모델 | 종류 | 필드 |
|---|---|---|
| `LLMConfig` | Protocol (`@runtime_checkable`) | `max_tokens: int`, `temperature: float \| None` — 공통 레버 구조적 마커 |
| `AnthropicConfig` | dataclass | `max_tokens=16384`, `temperature=None`, `thinking_enabled=False`, `thinking_budget=None` |
| `OpenAIConfig` | dataclass | `max_tokens=16384`, `temperature=None` |

`provider.config_type`으로 라우팅·검증하며, 미지정 시 `provider.config_type()` 기본값을 쓴다.

---

### 2.5 도구 — `tools/base.py` · `tools/orchestrator.py` → 상세 [02-tool-orchestration](02-tool-orchestration.md)

#### `ToolResult` — 도구 실행 결과

| 필드 | 타입 | 의미 |
|---|---|---|
| `data` | `Any` | 실행 결과 (문자열/구조화 데이터) |
| `is_error` | `bool` | 오류 여부 (기본 `False`) |

#### `Batch` (오케스트레이터 내부) — 파티셔닝 산출물

| 필드 | 타입 | 의미 |
|---|---|---|
| `is_concurrency_safe` | `bool` | `True`면 batch 내 블록 병렬 실행, `False`면 단독 순차 |
| `blocks` | `list[ContentBlock]` | 이 배치의 tool_use 블록들 |

---

### 2.6 프롬프트 — `api/prompts.py` → 상세 [03-llm-providers](03-llm-providers.md)

#### `SystemPrompt`

| 필드 | 타입 | 의미 |
|---|---|---|
| `text` | `str` | 조립 완료된 프롬프트 |

`__str__`을 구현해 `provider.complete(system_prompt=str(sp))`로 직접 전달된다. `assemble_system_prompt()`가 생성한다.

---

## ③ 변환·이송 흐름 (한눈에)

```
provider.complete()
   └─ AssistantResponse(content=[TextBlock|ToolUseBlock|ThinkingBlock], stop_reason, usage)
        │  _to_assistant_message()          ← 와이어 union → 내부 flat 변환
        ▼
   Message(content=[ContentBlock(flat)])    ← state.messages에 누적
        │  normalize_for_api()              ← 내부 → API wire dict (is_meta 제외·thinking verbatim)
        ▼
   list[dict]  → 다음 provider.complete()

[턴 경계 이송]
   LoopState(messages=[Message], turn_count)
        └─ Checkpoint(state)
              └─ json.dumps(checkpoint.to_dict())   ← 분산 재개 단위
```

---

## ④ 인터페이스 (데이터 모델 아님 — 참고)

아래는 dataclass가 아닌 인터페이스/ABC라 이 카탈로그 범위 밖이다.

| 심볼 | 위치 | 문서 |
|---|---|---|
| `Tool` (ABC) | `tools/base.py` | [02](02-tool-orchestration.md) |
| `LLMProvider` (ABC) | `api/provider.py` | [03](03-llm-providers.md) |
