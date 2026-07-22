# 06 — Par-Critical 불변식

## 불변식이란

"Par-critical 정합성"이란 **`tool_use` 블록과 `tool_result` 블록이 반드시 1:1로 대응해야 한다**는 규칙을 말한다. LLM API는 이전 assistant 메시지에 등장한 모든 `tool_use` 블록에 대해 다음 user 메시지에 매칭되는 `tool_result`가 존재하지 않으면 요청 자체를 거부한다. 이 제약은 에러 복구·병렬 실행 등 모든 실행 경로에서 깨지지 않아야 한다. 아래 표는 시스템 전반에 걸쳐 루프 정합성을 보장하는 핵심 불변식 목록이며, 각 항목은 실제 소스 코드의 구체적인 보장 지점과 연결된다.

---

## 불변식 목록

| 불변식 | 왜(깨지면) | 보장 위치 |
|---|---|---|
| 모든 `tool_use`에 대응 `tool_result` 존재 | LLM API가 요청 거부 | `core/loop.py:75` `yield_missing_tool_result_blocks()` — `LLMError` 발생 시 미완료 블록에 대해 합성 에러 `tool_result`를 백필 → [01-core-loop](01-core-loop.md) |
| 병렬 실행해도 결과는 `tool_use` 블록 원본 순서 | 쌍 매칭·재현성 파괴 | `tools/orchestrator.py:144` `run_tools()` — `asyncio.gather`는 인자 순서대로 결과를 반환하므로 완료 순서와 무관하게 블록 순서가 보존됨 → [02-tool-orchestration](02-tool-orchestration.md) |
| `tool_result` 메시지 `role="user"`, 첫 메시지 `user`, user/assistant 교대 | role 규칙 위반 시 API 거부 | `messages/types.py:91·105` — `create_tool_result_message()`·`create_user_message()`가 `role="user"`로 생성; `messages/normalize.py` — API 직렬화 시 role 태그 그대로 전달 → [05-messages](05-messages.md) |
| `step()`은 `state.messages` 전체를 API에 전송(윈도우 관리=호출자) | 임의 절사 시 컨텍스트 손실 | `core/loop.py:217` — `api_input_messages = with_turn_reminders(list(state_messages), …)` 전체 전달(턴-로컬 리마인더 추가만, 절사 없음); 오버플로는 `ContextOverflowError`로 호출자에게 전파 → `engine.compact()` 재시도 → [01-core-loop](01-core-loop.md)·[04-context-compaction](04-context-compaction.md) |
| thinking 활성 시 `temperature` 미전송 | Anthropic API가 두 파라미터 공존을 거부 | `api/anthropic_provider.py:148-154` `_build_params()` — `cfg.thinking_enabled`이면 `thinking` 파라미터만 설정, `elif`로 `temperature` 분기(상호 배타적) → [03-llm-providers](03-llm-providers.md) |
| 빈 `tools=[]`는 `tools` 필드 자체를 생략 | 일부 모델이 빈 배열을 에러로 처리 | `api/anthropic_provider.py:144-145` `if tools: params["tools"] = tools`; `api/openai_provider.py:142-143` `if oa_tools: params["tools"] = oa_tools` — 두 어댑터 모두 `_build_params()`에서 동일한 방어 처리 → [03-llm-providers](03-llm-providers.md) |
| thinking 블록은 verbatim 에코 | 생략 시 API 턴 시퀀스가 깨짐(Anthropic 요구사항) | `messages/normalize.py:57-64` — `block.type == "thinking"` 분기에서 `{"type": "thinking", "thinking": block.text}`를 그대로 삽입; 주석에 "omitting one breaks the API turn" 명시 → [05-messages](05-messages.md) |
| 캐시 프리픽스 바이트 안정성 (always-on 프롬프트 캐싱) | 변하면 해당 tier 캐시 무효화 — **비용 불변식**(정합성은 무해: 미스는 출력에 영향 없음) | `api/anthropic_provider.py` `_apply_cache_control()` — 마지막 system 블록 + 마지막/끝-2 메시지의 마지막 **영속** 블록에 `cache_control:{ephemeral}` 배치(트레일링 `<system-reminder>` 리마인더 스킵 — 비영속 블록의 breakpoint는 재사용 불가); `messages[-2]`가 안정 앵커(턴별 리마인더는 `messages[-1]`에만) → [03-llm-providers](03-llm-providers.md) |

---

## 교차 참조

각 불변식의 상세 구현은 소유 서브시스템 문서를 참조한다:

- **[01-core-loop](01-core-loop.md)** — `run_one_turn()`, `yield_missing_tool_result_blocks()`, 호출자 주도 컴팩션 흐름
- **[02-tool-orchestration](02-tool-orchestration.md)** — `run_tools()` 병렬 실행·순서 보존
- **[03-llm-providers](03-llm-providers.md)** — `_build_params()` thinking/temperature 상호 배타, 빈 tools 처리, `_apply_cache_control()` 캐시 breakpoint 배치
- **[04-context-compaction](04-context-compaction.md)** — `ContextOverflowError` 전파, `engine.compact()` 재시도 계약
- **[05-messages](05-messages.md)** — role 규칙, thinking 에코, `normalize_for_api()` 직렬화
