# 08 — 메모리 (영속 메모리 서브시스템)

> **관련 문서**: [00-overview](00-overview.md) · [01-core-loop](01-core-loop.md) · [02-tool-orchestration](02-tool-orchestration.md) · [03-llm-providers](03-llm-providers.md)

---

## ① 목적

세션 경계를 넘어 타입화된 fact(user/feedback/project/reference)를 장기화한다. **opt-in**이다 — `FridayAgent(memory=None)`(기본)이면 서브시스템이 장착되지 않고(도구·지침·인덱스 리마인더 전부 없음), 호출자가 `MemoryStore`를 주입하면 그 store와 `tools()`가 통째로 장착된다. (설계 이력: 본래 always-on 빌트인이었으나 실전 실측 — 전 세션 도구 호출 0회, 매 콜 상시 토큰 지출 — 을 근거로 opt-in으로 전환.)

## ② 담당 파일

| 경로 | 책임 | 핵심 심볼 |
|---|---|---|
| `friday_agent/memory/store.py` | 영속 + 도구 표면 인터페이스, 기본 파일 백엔드, 프롬프트 조각 조립(정적 지침 / 동적 인덱스 리마인더) | `MemoryStore`, `FileMemoryStore`, `MemoryEntry`, `IndexEntry`, `MemoryType`, `MEMORY_INSTRUCTIONS`, `build_memory_reminder` |
| `friday_agent/memory/tool.py` | 에이전트 표면(저장/읽기/삭제) | `MemorySave`, `MemoryRead`, `MemoryDelete` |

## ③ 단일 주입 seam — `MemoryStore`

`MemoryStore`(ABC)가 **영속(save/read/delete/load_index) + `tools()`**를 모두 소유한다. 백엔드만 교체하려면 영속 4메서드만 구현(기본 `tools()` 상속), 도구까지 교체하려면 `tools()`를 오버라이드한다. 기본 도구는 `memory_save`/`memory_read`/`memory_delete`이며 일반 `ToolResult`만 반환한다(메모리는 LoopState가 아니라 Store에 산다).

## ④ 엔진 통합 (루프 무변경)

`FridayAgent.__init__`은 `memory=` 인자로 store가 주입된 경우에만 `store.tools()`를 빌트인·호출자 도구와 함께 등록한다(이름 충돌 시 `ValueError`; `memory=None`이면 메모리 도구·프롬프트 모두 없음). store가 장착되면 `step()`은 메모리 프롬프트를 **정적/동적으로 분리 주입**한다: 정적 지침 `MEMORY_INSTRUCTIONS`는 base 시스템 프롬프트 앞에 오고(범용→구체 — 도메인 규칙이 recency 우위; 세션 내 byte-stable이라 캐시되는 system 프리픽스에 안전), 라이브 인덱스는 매 턴 `build_memory_reminder()`로 async 렌더돼 turn-local `<system-reminder>`로 `messages[-1]`에만 실린다(`run_one_turn`의 `turn_reminders` 경로, `LoopState` 비영속). 빈 store면 리마인더 블록 자체가 생기지 않는다(빈 상태 안내는 지침이 담당). `assemble_system_prompt`·`LoopState`·orchestrator는 불변. `compact()`는 메모리 프롬프트를 주입하지 않아 인덱스가 요약에 새지 않는다. 프롬프트 캐싱(always-on) 관점: `memory_save`/`delete`로 인덱스가 바뀌어도 system 프리픽스와 대화 히스토리 캐시는 그대로 살아남고, 바뀌는 것은 breakpoint 밖의 리마인더 블록뿐이다 — 인덱스를 system에 두면 저장 1회가 대화 전체 캐시를 무효화한다(구 설계에서 실측된 비용).

## ⑤ 분산 안전

`MemoryStore`는 `LoopState`에 직렬화되지 않는다(provider/tools와 동일 컨테이너-로컬 재주입). 컨테이너 B가 store로 `FridayAgent`를 재구성하면 리마인더가 재렌더돼 인덱스가 fresh 반영된다. 기본 `FileMemoryStore`는 단일 프로세스/로컬 편의 — 진짜 분산 영속은 외부 Storage 기반 `MemoryStore`를 주입한다.

## ⑥ 비목표

Sonnet prefetch 랭킹 · 백그라운드 포크 추출 · 팀 메모리/시크릿 스캔/scope 태그 제외. (인덱스는 `step()`마다 `build_memory_reminder`가 재렌더한다 — 캐시 없음.) `search`는 기본 도구가 아니며 커스텀 store가 `tools()`로 노출할 수 있다.
