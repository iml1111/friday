# 03 — LLM 프로바이더 경계

LLM 백엔드 교체 경계를 정의한다. 모든 completion 호출은 이 레이어를 통과하며, 벤더별 응답은 `AssistantResponse`로 정규화된다.

---

## ① 목적

`api/` 패키지는 세 가지 책임을 단일 교체 경계에 집약한다.

1. **추상 인터페이스** — `LLMProvider(ABC)` + 공통 타입(`AssistantResponse`, `StopReason`, `ContentBlock`, `TokenUsage`)
2. **에러 계층** — 5개 클래스로 벤더 예외를 정규화해 루프가 벤더 SDK에 의존하지 않도록 한다.
3. **벤더 어댑터** — Anthropic / OpenAI 각각이 같은 인터페이스를 구현한다.

`core/loop.py`와 `context/compact.py`는 `LLMProvider.complete()`만 호출한다. 어댑터를 교체해도 루프 코드는 변경이 없다.

---

## ② 담당 파일 표

| 경로 | 책임 | 핵심 심볼 |
|---|---|---|
| `api/provider.py` | 추상 경계·공통 타입·5-클래스 에러 | `LLMProvider`, `LLMConfig`, `AssistantResponse`, `StopReason`, `ContentBlock`(union) |
| `api/configs.py` | 벤더별 호출 config | `AnthropicConfig`, `OpenAIConfig` |
| `api/anthropic_provider.py` | Anthropic 어댑터 | `AnthropicProvider` |
| `api/openai_provider.py` | OpenAI 어댑터 | `OpenAIProvider` |
| `api/prompts.py` | 시스템 프롬프트 조립 | `SystemPrompt`, `assemble_system_prompt()` |

---

## ③ 핵심 동작 / 타입

### LLMProvider

`api/provider.py:107` — `LLMProvider(ABC, Generic[ConfigT])`는 `complete()` 추상 메서드 하나를 강제한다.

| 멤버 | 시그니처 | 설명 |
|---|---|---|
| `complete()` | `async (messages, system_prompt, tools, config) -> AssistantResponse` | 단일 completion 호출. 어댑터가 벤더 SDK를 호출하고 응답을 정규화한다. |

클래스 속성 `config_type: type[ConfigT]`는 어댑터가 반드시 설정해야 한다. `FridayAgent`이 config/provider 불일치 검증과 기본 config 생성(`provider.config_type()`)에 사용한다.

### 공통 타입

`api/provider.py:34` — `ContentBlock = Union[TextBlock, ToolUseBlock, ThinkingBlock]`

- `TextBlock(type, text)` — 텍스트 응답 블록
- `ToolUseBlock(type, id, name, input)` — 도구 호출 블록. `input`은 항상 `dict`
- `ThinkingBlock(type, thinking, signature)` — extended thinking 블록 (Anthropic 전용)

`api/provider.py:55` — `AssistantResponse(content, stop_reason, usage, id, model)`

`api/provider.py:37` — `StopReason` enum:

| 값 | 의미 |
|---|---|
| `END_TURN` | 정상 종료 |
| `TOOL_USE` | 도구 호출 요청 |
| `MAX_TOKENS` | 출력 토큰 상한 도달 |
| `CONTEXT_WINDOW_EXCEEDED` | 컨텍스트 윈도우 초과 (방어적 매핑; 실제 오버플로는 예외로 전달) |

`api/provider.py:46` — `TokenUsage(input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens)`. 캐싱을 지원하지 않는 백엔드의 cache 필드는 0.

### LLMConfig (Protocol)

`api/provider.py:90` — `@runtime_checkable class LLMConfig(Protocol)`. 공통 레버 `max_tokens: int`, `temperature: float | None`만 선언한다. 벤더 config는 공유 베이스 없이 완전히 분리된다(설계 근거 ⑦ 참조).

### 벤더 Config

`api/configs.py` — 벤더 SDK를 임포트하지 않는 순수 dataclass. config 생성·검사·라우팅은 SDK 로딩 없이 가능하다.

```
AnthropicConfig(max_tokens=16384, temperature=None, thinking_enabled=False, thinking_budget=None)
OpenAIConfig(max_tokens=16384, temperature=None)
```

### 5-클래스 에러 계층

`api/provider.py:64–86` — 모든 벤더 예외는 루프에 도달하기 전에 다섯 클래스 중 하나로 매핑된다.

```
LLMError (base)
├── RateLimitError       — 429 과호출
├── ContextOverflowError — 컨텍스트 윈도우 초과 → 호출자 compact 트리거
├── AuthError            — API 키 만료 등 인증 실패
└── TransientError       — 네트워크 타임아웃, 5xx 등 일시적 오류
```

`ContextOverflowError`는 어댑터가 400 응답을 판정해 raise하고, 호출자(`FridayAgent.step()` 사용 측)가 `engine.compact(state)` 후 재시도한다.

### provider 생성

라이브러리는 provider 생성 팩토리를 제공하지 않는다 — 어댑터(`AnthropicProvider`/`OpenAIProvider`)를 직접 인스턴스화한다(`api_key` 필수, 외부 주입; 빈 값이면 어댑터가 `ValueError`). model prefix(`claude-`/`gpt-`) 기반 라우팅이 필요한 caller는 경계 계층에서 직접 처리한다(예: `scripts/_env.py`의 `create_provider(model, api_key=...)` — 매칭 브랜치에서만 어댑터 모듈을 lazy import).

### 시스템 프롬프트 조립 (prompts.py)

`api/prompts.py:12` — `SystemPrompt(text)`는 `__str__`을 구현해 `LLMProvider.complete(system_prompt=str(sp))`에 직접 전달된다.

- `assemble_system_prompt(system_prompt)` — base prompt 뒤에 `GENERAL_AGENT_GUIDANCE`를 주입한 뒤 `SystemPrompt`로 래핑한다. base가 비면 guidance만 반환.

---

## ④ 어댑터별 차이 표

벤더 경계의 핵심 — 두 어댑터가 어디서 다르게 동작하는지를 기술한다.

| 항목 | Anthropic (`api/anthropic_provider.py`) | OpenAI (`api/openai_provider.py`) |
|---|---|---|
| **tool 입력 파싱** | SDK가 `tool_use.input`을 dict로 pre-parse. 재파싱 금지 (`_normalize_block:232`) | `tool_calls[].function.arguments`가 JSON 문자열 → `json.loads` (`_parse_arguments:329`) |
| **메시지 형식** | content-block 그대로 전달 | `tool_use`→`tool_calls`, `tool_result`→`{"role":"tool"}`, system→선두 메시지, thinking 드롭 (`_to_openai_messages:182`) |
| **빈 tools** | 필드 자체 생략 (`_build_params:171`) | 동일 — 필드 자체 생략 (`_build_params:172`) |
| **thinking** | `thinking_enabled=True` 시 `temperature` 미전송 (`_build_params:175`) | thinking 미지원 |
| **오버플로 판정** | 400/413 + 메시지 시그널 검사 (`_is_context_overflow:316`) | 400/413 + `body.error.code=="context_length_exceeded"` 또는 메시지 검사 (`_is_context_overflow:403`) |
| **stop_reason 미매핑** | `END_TURN` 폴백 (`_map_stop_reason:258`) | `END_TURN` 폴백 (`_map_stop_reason:347`) |

---

## ⑤ 의존성

### 외부 의존성

- **표준 라이브러리** — `abc`, `dataclasses`, `enum`, `typing`, `json`
- **`anthropic` SDK** — `AnthropicProvider`에서만 import (lazy)
- **`openai` SDK** — `OpenAIProvider`에서만 import (lazy)

`api/configs.py`는 어떤 벤더 SDK도 import하지 않는다 — config 생성·검사는 SDK 없이 가능하다.

### 내부 의존성

```
core/loop.py         → provider.complete()  호출
context/compact.py   → provider.complete()  호출 (요약 생성 시)
core/engine.py       → provider.config_type 검증
```

`api/` 패키지 내부 의존 방향: `anthropic_provider` / `openai_provider` → `provider` ← `configs`.

---

## ⑥ 유지보수 주의점

다음 불변 조건은 어댑터를 수정할 때 반드시 유지해야 한다. 자세한 목록은 [06-invariants](06-invariants.md) 참조.

1. **thinking 시 temperature 금지** — `AnthropicConfig.thinking_enabled=True`이면 `temperature` 파라미터를 API에 전송하면 안 된다 (`anthropic_provider.py:175–181`).
2. **빈 tools 생략** — `tools=[]`를 API에 보내면 일부 모델에서 요청 거부. 두 어댑터 모두 빈 리스트일 때 `tools` 필드를 생략한다.
3. **`ContextOverflowError` 전파** — 어댑터가 400을 판정해 raise하면, `core/loop.py`가 이를 잡지 않고 호출자에게 전파한다. 호출자는 `engine.compact(state)` 후 재시도한다. 컨텍스트 compaction 흐름은 [04-context-compaction](04-context-compaction.md) 참조.
4. **OpenAI 인수 파싱** — `_parse_arguments`는 JSON 파싱 실패 시 빈 dict `{}`를 반환한다. 어댑터를 수정할 때 파싱 예외를 루프 밖으로 누출하지 않도록 주의.

---

## ⑦ 설계 근거 (Why)

### 벤더 config 완전 분리

`AnthropicConfig`와 `OpenAIConfig`는 공유 베이스 클래스 없이 완전히 분리된 dataclass다. 두 config가 서로 다른 파라미터 집합(`thinking_enabled`, `thinking_budget` vs 없음)을 가지며, 공통 베이스가 있으면 한 벤더 전용 필드가 다른 벤더 config에도 노출되는 문제가 생긴다. `LLMConfig(Protocol)`을 런타임 체크 가능한 구조적 마커로 두고, 공통 레버(`max_tokens`, `temperature`)만 선언하는 방식으로 타입 안전성을 유지하면서 완전한 분리를 달성한다.

### 어댑터 모듈 직접 import

필요한 어댑터 모듈만 직접 import하면(예: `from friday_agent.api.anthropic_provider import AnthropicProvider`), Claude만 사용하는 경우 `openai` 패키지가 설치되지 않아도 동작한다 — 의존성 격리와 임포트 비용 절감을 동시에 달성한다. 각 벤더 SDK(`anthropic`/`openai`)도 해당 어댑터 모듈 내부에서만 import된다. (caller가 prefix 라우팅을 쓸 때도 `scripts/_env.py`의 `create_provider`가 매칭 브랜치에서만 어댑터를 import해 동일한 격리를 유지한다.)

### 5-클래스 에러 계층

루프(`core/loop.py`)가 벤더 SDK 예외 클래스를 직접 잡으면, 어댑터 교체 시 루프 코드도 수정해야 한다. 어댑터가 모든 예외를 5개 클래스 중 하나로 변환해 raise하면, 루프는 벤더에 무관하게 동일한 핸들링이 가능하다.
