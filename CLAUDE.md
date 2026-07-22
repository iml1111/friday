# CLAUDE.md

## 프로젝트 목적

여러 에이전트 루프(agentic loop) 동작 구조를 분석해, **클라우드/서버 사이드에서 에이전트 루프를 구동**하기 위한 **도메인 무관 + LLM 무관(LLM-agnostic)** SDK다. 프로그래밍 외 다양한 목적의 AI 에이전트를 만드는 토대로 삼는 것이 목표다.

## 현재 상태

**구현 완료** — `friday_agent/` 패키지에 Phase 1~3(최소 루프 · 도구 오케스트레이션 · 컨텍스트 관리)이 모두 구현돼 있다. 실 백엔드 어댑터(Anthropic · OpenAI) + 모델 prefix 라우팅, stateless 분산 재개(LoopState)까지 포함한다. 아키텍처 문서(`docs/architecture/`)가 권위 기준이다.

- 설치: `pip install -e ".[dev]"`  ·  테스트(키 불필요, fake provider): `python -m pytest`
- 실 API 검증(토큰 비용 발생): `LLM_MODEL=<모델ID> python scripts/verify/verify_p2.py` (P2~P4)
- 코드 진입점 · 모듈 지도 · 읽는 순서는 `docs/architecture/00-overview.md`(특히 `#모듈-지도`) 참조.

## 아키텍처 문서 = 단일 진실 공급원

`docs/architecture/`는 현재 구현체(`friday_agent/`)를 서브시스템별로 설명하는 권위 문서다. 구현·유지보수는 이 문서를 기준으로 한다.

**이해를 위한 읽는 순서** (이하 모두 `docs/architecture/` 하위):
1. `00-overview.md` — 목적·큰그림·모듈지도·스코프
2. `01-core-loop.md` — 단일 턴 루프·종료/복구·재개
3. `02-tool-orchestration.md` — 파티셔닝·병렬 실행
4. `03-llm-providers.md` — LLM 경계·어댑터
5. `04-context-compaction.md` — 호출자 주도 compact
6. `05-messages.md` — 메시지/변환
7. `06-invariants.md` — par-critical 불변식
8. `07-data-models.md` — 전 데이터 모델 카탈로그
9. `08-memory.md` — 영속 메모리 서브시스템(opt-in, `MemoryStore` 주입)

## 아키텍처 큰 그림

핵심은 `run_one_turn()`(단일 턴) 위에서 **호출자가 도는** 턴 루프다 (`docs/architecture/01-core-loop.md`, `02-tool-orchestration.md` 참조):

```
호출자 (분산 오케스트레이터 / 서버 사이드)
        │  LoopState(messages=[...])  ← 첫 턴은 호출자가 구성
        ▼
engine.step(state)       ┌─ run_one_turn 1회 (비동기 제너레이터) ───────┐
        │                │ 1. state.messages → LLM API 호출             │
        │                │ 2. 응답 수신 → stop_reason 분기               │
        │                │      end_turn  → Terminal (루프 종료)        │
        │                │      tool_use  → 도구 실행 → tool_result      │
        │                │ → Message…들을 순서대로 stream yield          │
        │                │ → 마지막에 LoopState | Terminal               │
        │                │   sentinel 1개 yield                          │
        │                └──────────────────────────────────────────────┘
        │   ContextOverflowError 발생 시 → 호출자가 engine.compact(state) 후 재시도
        ▼
호출자가 `async for`로 소비: 마지막 sentinel이 LoopState면 그대로 step()을 다시 호출, Terminal이면 종료.
```

> 구현 메모: 라이브러리는 단일 턴 실행 `FridayAgent.step()`만 노출한다 — batch 드라이버(`query()`)·완주 진입점(`submit_message`)·`max_turns`는 제거됐다(에이전트 루프 명세 `02`의 while-true 드라이버를 호출자에게 외부화). 턴 경계에서 직렬화 가능한 `LoopState`를 그대로 방출해 **stateless 분산 재개**(`FridayAgent.step()` + 타입의 `to_dict()`/`from_dict()`)를 지원한다.

핵심 데이터 구조 (`docs/architecture/01-core-loop.md`·`05-messages.md`):
- **`Message` + `ContentBlock`** — 별도 *Message 클래스 유니온은 없다. 단일 `Message` dataclass(`type` 태그: `user`/`assistant`/`system`)에 flag 필드(`is_compact_summary`/`is_meta`/`is_api_error_message`)뿐이다. 블록도 단일 flat `ContentBlock` dataclass(`type`: `text`/`tool_use`/`tool_result`/`thinking`)로 표현한다.
- **Terminal** — **`run_one_turn`이 실제로 방출하는** 루프 종료 사유(`completed`, `model_error`). 컨텍스트 오버플로는 `ContextOverflowError`로 호출자에게 전파되므로 `prompt_too_long` 종료 사유는 없다. 턴 경계의 "계속"은 `LoopState`를 그대로 방출해 표현한다(`Terminal`과 대비).
- **LoopState** (구현) — 직렬화 가능한 루프 상태(messages·turn_count)이자 턴 경계 "계속" 재개 sentinel. 분산 재개의 이송 단위이며 JSON serde(`to_dict()`/`from_dict()`)를 자체 보유한다 (`core/state.py`·`messages/types.py`). 이송 단위는 `json.dumps(loopstate.to_dict())`.

## 구현 스코프 헌장 (반드시 준수)

아키텍처 문서는 의도적으로 **"에이전트 루프 알고리즘의 본질"** 만 기술한다 — 분석한 실제 에이전트 루프 구현들보다 추상 수준이 높다. 아래 제외 항목은 그런 구현들에 존재하지만 본 SDK 범위가 아니다. **임의로 다시 추가하지 말 것** (상세: `docs/architecture/00-overview.md`):

| 포함 (par 수준으로 구현) | 제외 (의도적) |
|---|---|
| while-true 루프 + stop_reason 분기, 모든 종료/복구 경로 | 서브에이전트 위임 |
| 도구 파티셔닝 + 동시성 (병렬/순차 배치) | 스트리밍 / 점진 표시 UX |
| 외부 compact + 오버플로 전파(호출자 주도 compact) | Snip·Micro·Collapse 등 컨텍스트 최적화 |
| 시스템 프롬프트 조립 machinery | Model fallback · Beta 헤더 |
| LLM-agnostic 프로바이더 경계 | 벤더 빌드 모드(ant/REPL/SIMPLE) |
| 프롬프트 캐싱 (시스템+도구+대화 히스토리, always-on; Anthropic 명시 breakpoint / OpenAI 자동) | mega-turn(>20블록) 중간 breakpoint · TTL 설정 · OpenAI `prompt_cache_key` |

**Par-critical 정합성**: `tool_use`↔`tool_result` 쌍이 깨지면 LLM API가 요청을 거부한다. 복구·병렬 실행 어느 경로에서도 이 정합성을 보존해야 한다 (상세: `docs/architecture/06-invariants.md`).

## LLM 추상화 경계

LLM 백엔드 교체는 **3개 인터페이스**로 한정된다 (`docs/architecture/03-llm-providers.md`):
- **`LLMProvider`** — 유일한 *필수* 교체 지점. completion 호출 + 응답을 `AssistantResponse`로 정규화.
- **`ToolExecutor`** / **`ContextManager`** — 대부분 범용 (LLM 호출부만 교체).
- **메모리는 별도 서브시스템**(LLM 경계 아님) — `friday_agent/memory/`의 `MemoryStore`(영속 + `tools()` 소유). opt-in(기본 미장착)이며 store 주입 시 장착. 상세: `docs/architecture/08-memory.md`.

벤더 경계와 어댑터별 차이는 `docs/architecture/03-llm-providers.md`의 어댑터 차이 표 참조.

## 구현 로드맵

코드는 Phase 순서로 구현됐다(각 Phase 독립 검증):
- **Phase 1 — 최소 루프**: 단일 도구 호출 → 결과 → 응답. `messages/types.py`, `core/state.py`, `tools/base.py`, `tools/builtin/`, `api/provider.py`+어댑터, `core/loop.py`.
- **Phase 2 — 도구 오케스트레이션**: 파티셔닝 + 병렬 실행(`asyncio.gather` + `Semaphore`) + 블록 순서 보존. `tools/orchestrator.py`.
- **Phase 3 — 컨텍스트 관리**: 외부 compact + 오버플로 전파(호출자 주도). `step()`이 `ContextOverflowError`를 던지면 호출자가 `engine.compact(state)`로 축소 후 재시도한다. `context/compact.py`(요약 전용), `messages/normalize.py`. 컴팩션 동작 상세는 `docs/architecture/04-context-compaction.md` 참조.

실제 패키지 구조(`friday_agent/`): `core/`(loop·engine·state) · `tools/`(base·orchestrator·builtin) · `context/`(compact — 요약 전용, recovery.py 없음) · `api/`(provider·configs·anthropic_provider·openai_provider·prompts) · `messages/`(types·normalize) · `memory/`(store·tool). 파일별 책임은 `docs/architecture/00-overview.md#모듈-지도` 참조.

## 타겟 스택 & 검증

- **Python 3.11+**, `anthropic` + `openai` SDK + `pydantic` + `anyio`.
- API 키: **외부 주입 전용**(필수). 라이브러리는 환경변수를 읽지 않는다 — provider 생성 시 어댑터(`AnthropicProvider(api_key=...)`/`OpenAIProvider(api_key=...)`)에 직접 넘긴다. 키 해석(env→인자)과 모델 prefix(claude-/gpt-)→어댑터 라우팅은 모두 경계 계층 `scripts/_env.py`(`resolve_api_key(model)`·`create_provider(model, api_key=...)`·`create_config(model, ...)`)가 담당한다 — 라이브러리는 라우팅 팩토리를 제공하지 않는다. **`FridayAgent`은 provider를 직접 받는다(필수)** — model/api_key 인자는 없다. 공개 API 면: `engine.step(state)` (단일 턴 비동기 제너레이터 — Message들을 순서대로 yield하고 마지막에 `LoopState | Terminal` sentinel 1개를 yield) + `engine.compact(state)` (호출자 주도 compact). 호출 config는 벤더 config를 직접 입력한다(예: `AnthropicConfig(max_tokens=...)` 또는 `provider.config_type(max_tokens=...)`; 미지정 시 `provider.config_type()` 기본값). `context_window`·`max_output_tokens`는 어댑터 생성자, `max_concurrency`는 `FridayAgent(...)`. 컨텍스트 주입 면은 단순하게 유지한다 — 정적 콘텐츠는 `system_prompt` 문자열 하나(섹션 조합은 호출자 몫), 엔진 수준 동적 주입 훅은 없다.
- **빌트인 todo 항상-주입**: `TodoWrite` 도구와 todo 가이던스(`TODO_GUIDANCE`)는 항상 빌트인으로 자동 등록·주입된다(호출자 주입 불필요, opt-out 없음). 호출자가 같은 이름의 도구를 넘기면 `FridayAgent.__init__`이 `ValueError`로 거부한다.
- **memory는 opt-in**: `FridayAgent(memory=None)`(기본)이면 메모리 서브시스템 미장착 — 도구 3종(`memory_save`/`memory_read`/`memory_delete`)·지침·인덱스 리마인더 전부 없음. `FridayAgent(..., memory=FileMemoryStore())` 등 store를 명시 주입해야 장착된다(store가 곧 도구 표면). 장착 시 주입은 정적/동적 분리 — `MEMORY_INSTRUCTIONS`(정적 지침)는 `step()`에서 시스템 프롬프트에, 라이브 인덱스는 `build_memory_reminder`로 turn-local `<system-reminder>`(`messages[-1]` 전용, 비영속)로 실린다(캐시 프리픽스 보존). 코어 루프·`LoopState` serde는 불변(store는 컨테이너-로컬 재주입).
- Phase 검증: 단일 도구(P1) → 병렬 도구 배치(P2) → `ContextOverflowError` → `engine.compact(state)` 호출자 복구(P3).

## 구현 시 핵심 함정

- `step()`은 `state.messages`를 그대로 API에 보낸다. 컨텍스트 윈도우 관리는 호출자 책임 — `step()`이 `ContextOverflowError`를 던지면 `engine.compact(state)`로 축소 후 재시도한다.
- `tool_result` 메시지의 `role`은 반드시 `"user"`, 첫 메시지도 user여야 한다 (role 교대 규칙).
- `tool_use.input`은 (Anthropic SDK 기준) 이미 dict로 파싱돼 옴 — 직접 JSON 파싱 금지. (단 OpenAI 어댑터는 JSON 문자열 arguments를 파싱해 dict로 정규화한다 — 벤더별 차이.)
- thinking 활성 시 `temperature` 전송 금지. 빈 `tools=[]`는 필드 자체를 생략.
- 프롬프트 캐싱은 **always-on**(opt-out·config 노브 없음): Anthropic 어댑터 `_apply_cache_control`이 매 호출 마지막 system 블록(=tools+system) + 마지막/끝-2번째 메시지의 마지막 **영속** 블록에 `cache_control:{ephemeral}`을 배치한다(시스템+도구 프리픽스 + 대화 히스토리 캐시). 트레일링 `<system-reminder>` 리마인더는 스킵 — 비영속 블록의 breakpoint는 재사용 불가. `messages[-2]`가 안정 앵커 — 턴별 리마인더(todo·메모리 인덱스)가 `messages[-1]`에만 붙기 때문. OpenAI는 자동 캐싱이라 어댑터 무변경.
- 병렬 실행해도 결과는 **tool_use 블록 순서대로** yield (`asyncio.gather`는 인자 순서 보장).
