# 00. 개요 (Overview)

`friday_agent/` 패키지의 아키텍처 전체를 한눈에 파악하기 위한 진입점 문서다.  
상세 구현은 하위 번호 문서(01–08)에서 다룬다.

---

## 프로젝트 목적

여러 에이전트 루프(agentic loop) 동작 구조를 분석해, **클라우드/서버 사이드에서 에이전트 루프를 구동**하기 위한  
**도메인 무관 + LLM 무관(LLM-agnostic)** SDK다.  
프로그래밍 외 다양한 목적의 AI 에이전트를 만드는 토대로 삼는 것이 목표다.

구현체: `friday_agent/` 패키지 (Python 3.11+, `anthropic` + `openai` SDK + `pydantic` + `anyio`).

---

## 호출자 주도 턴 루프 큰 그림

핵심 설계 원칙은 **라이브러리가 단일 턴 실행(`FridayAgent.step()`)만 노출하고, while-true 드라이버를 호출자에게 외부화**한다는 점이다. 이를 통해 분산 오케스트레이터·서버리스 등 다양한 서버 사이드 실행 컨텍스트에서 동일한 엔진을 재사용할 수 있다(로컬 예시 드라이버: `scripts/run_agent.py`).

```
호출자 (분산 오케스트레이터 / 서버 사이드)
   │  LoopState(messages=[...])
   ▼
async for item in engine.step(state):   ← AsyncGenerator
   │   run_one_turn(state)이 내부에서 구동
   │     1. normalize → provider.complete()
   │     2. stop_reason 분기
   │          end_turn → Terminal(completed)
   │          tool_use → run_tools → tool_result
   │
   ├─ yield: AssistantMessage          ← 응답 도착 시 즉시
   ├─ yield: tool_result Message…      ← 각 도구 결과
   └─ yield: LoopState | Terminal      ← 마지막 sentinel 1개
        ContextOverflowError 발생 시 → 호출자가 engine.compact(state) 후 재시도

item이 LoopState면 그대로 step() 재호출, Terminal이면 종료.
```

턴 경계에서 직렬화 가능한 `LoopState`를 그대로 방출해 **stateless 분산 재개**를 지원한다.

또한 `FridayAgent`는 **TodoWrite(todo 추적)**와 **메모리(`MemoryStore`)**를 호출자 배선 없이 항상 빌트인으로 등록·주입한다 — 상세는 [02-tool-orchestration](02-tool-orchestration.md)·[08-memory](08-memory.md) 참조.

---

## 모듈 지도

| 서브시스템 | 파일 | 문서 |
|---|---|---|
| `core/` | `loop.py`·`engine.py`·`state.py` | [01-core-loop](01-core-loop.md) |
| `tools/` | `base.py`·`orchestrator.py`·`builtin/example_tool.py`·`builtin/todo_write.py` | [02-tool-orchestration](02-tool-orchestration.md) |
| `api/` | `provider.py`·`configs.py`·`anthropic_provider.py`·`openai_provider.py`·`prompts.py` | [03-llm-providers](03-llm-providers.md) |
| `context/` | `compact.py` | [04-context-compaction](04-context-compaction.md) |
| `messages/` | `types.py`·`normalize.py` | [05-messages](05-messages.md) |
| (횡단) | par-critical 불변식 | [06-invariants](06-invariants.md) |
| (횡단) | 전 데이터 모델 카탈로그 | [07-data-models](07-data-models.md) |
| `memory/` | `store.py`·`tool.py` — 영속 메모리 서브시스템. always-on 빌트인, `MemoryStore` 주입으로 교체 | [08-memory](08-memory.md) |

---

## 읽는 순서

00 → 01 → 02 → 03 → 04 → 05 → 06 (→ 07 참조) (→ 08 메모리)

| 순서 | 문서 | 핵심 내용 |
|---|---|---|
| 00 | 이 문서 | 목적·큰 그림·모듈 지도·스코프 |
| 01 | [01-core-loop](01-core-loop.md) | `run_one_turn()`, `FridayAgent.step()`, 상태 흐름 |
| 02 | [02-tool-orchestration](02-tool-orchestration.md) | 도구 파티셔닝·병렬/순차 실행·순서 보존 |
| 03 | [03-llm-providers](03-llm-providers.md) | `LLMProvider` 추상화·Anthropic·OpenAI 어댑터 |
| 04 | [04-context-compaction](04-context-compaction.md) | `ContextOverflowError`·`engine.compact()`·요약 전략 |
| 05 | [05-messages](05-messages.md) | Message 유니온 타입·`normalize_for_api()` |
| 06 | [06-invariants](06-invariants.md) | par-critical 불변식·`tool_use`↔`tool_result` 정합성 |
| 07 | [07-data-models](07-data-models.md) | 전 데이터 모델 필드·직렬화 경계 카탈로그 (참조용) |
| 08 | [08-memory](08-memory.md) | 영속 메모리 서브시스템·`MemoryStore` 교체 seam·분산 안전 |

---

## 구현 스코프 헌장

명세는 의도적으로 **"에이전트 루프 알고리즘의 본질"** 만 기술한다. 아래 제외 항목은 분석한 실제 구현들에 존재하지만 본 SDK 범위가 아니다. **임의로 다시 추가하지 말 것**.

| 포함 (par 수준으로 구현) | 제외 (의도적) |
|---|---|
| while-true 루프 + stop_reason 분기, 모든 종료/복구 경로 | 서브에이전트 위임 |
| 도구 파티셔닝 + 동시성 (병렬/순차 배치) | 스트리밍 / 점진 표시 UX |
| 외부 compact + 오버플로 전파(호출자 주도 compact) | Snip·Micro·Collapse 등 컨텍스트 최적화 |
| 시스템 프롬프트 조립 machinery | Model fallback · Beta 헤더 · 프롬프트 캐싱 구체 |
| LLM-agnostic 프로바이더 경계 | 벤더 빌드 모드(ant/REPL/SIMPLE) |

**Par-critical 정합성**: `tool_use`↔`tool_result` 쌍이 깨지면 LLM API가 요청을 거부한다. 복구·병렬 실행 어느 경로에서도 이 정합성을 보존해야 한다. 상세는 [06-invariants](06-invariants.md) 참조.

---

## 검증·테스트 진입점

```bash
# 키 불필요 — fake provider로 전체 테스트
python -m pytest

# 실 API 검증 (실 백엔드 호출 — 토큰 비용 발생)
LLM_MODEL=<모델ID> python scripts/verify/verify_p2.py   # 도구 오케스트레이션
LLM_MODEL=<모델ID> python scripts/verify/verify_p3.py   # 컨텍스트 오버플로 · compact 복구
LLM_MODEL=<모델ID> python scripts/verify/verify_p4.py   # 실 백엔드 엔드투엔드 · 어댑터 교체 실증

# 로컬 예시 드라이버 실행
python scripts/run_agent.py
```

---

## 설계 근거(Why) 요약

라이브러리는 단일 턴 `FridayAgent.step()`만 노출하고 while-true 드라이버를 호출자에게 외부화했다. 이 결정의 핵심 이점은 두 가지다.

1. **stateless 분산 재개** — 턴 경계마다 직렬화 가능한 `LoopState`를 그대로 방출하므로, 프로세스 재시작이나 분산 큐 환경에서도 상태를 복원할 수 있다. JSON serde는 타입(`LoopState`/`Message`)의 `to_dict()`/`from_dict()` 메서드가 담당한다.
2. **컨텍스트 관리 책임 분리** — `ContextOverflowError`를 호출자에게 전파함으로써 라이브러리 내부를 단순하게 유지하고, compact 전략(타이밍·요약 방식)을 호출자가 직접 제어하게 한다 (`engine.compact(state)`).
