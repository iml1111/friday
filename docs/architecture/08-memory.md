# 08 — 메모리 (영속 메모리 서브시스템)

> **관련 문서**: [00-overview](00-overview.md) · [01-core-loop](01-core-loop.md) · [02-tool-orchestration](02-tool-orchestration.md) · [03-llm-providers](03-llm-providers.md)

---

## ① 목적

세션 경계를 넘어 타입화된 fact(user/feedback/project/reference)를 장기화한다. **always-on 빌트인**이며, 호출자가 자신의 `MemoryStore`를 주입하면 기본 store와 도구가 통째로 대체된다.

## ② 담당 파일

| 경로 | 책임 | 핵심 심볼 |
|---|---|---|
| `friday_agent/memory/store.py` | 영속 + 도구 표면 인터페이스, 기본 파일 백엔드, 프롬프트 섹션 조립(지침 + 자동 인덱스) | `MemoryStore`, `FileMemoryStore`, `MemoryEntry`, `IndexEntry`, `MemoryType`, `MEMORY_INSTRUCTIONS`, `build_memory_section` |
| `friday_agent/memory/tool.py` | 에이전트 표면(저장/읽기/삭제) | `MemorySave`, `MemoryRead`, `MemoryDelete` |

## ③ 단일 주입 seam — `MemoryStore`

`MemoryStore`(ABC)가 **영속(save/read/delete/load_index) + `tools()`**를 모두 소유한다. 백엔드만 교체하려면 영속 4메서드만 구현(기본 `tools()` 상속), 도구까지 교체하려면 `tools()`를 오버라이드한다. 기본 도구는 `memory_save`/`memory_read`/`memory_delete`이며 일반 `ToolResult`만 반환한다(메모리는 LoopState가 아니라 Store에 산다).

## ④ 엔진 통합 (루프 무변경)

`FridayAgent.__init__`이 기본 `FileMemoryStore`를 세팅하고 `store.tools()`를 빌트인·호출자 도구와 함께 등록한다(이름 충돌 시 `ValueError`). `step()`은 매 턴 `build_memory_section()`을 async로 조립해(턴별 재구성, 캐시 없음) base 시스템 프롬프트 뒤에 덧붙인다. `run_one_turn`·`assemble_system_prompt`·`LoopState`·orchestrator는 불변. `compact()`는 메모리 섹션을 주입하지 않아 인덱스가 요약에 새지 않는다.

## ⑤ 분산 안전

`MemoryStore`는 `LoopState`에 직렬화되지 않는다(provider/tools와 동일 컨테이너-로컬 재주입). 컨테이너 B가 store로 `FridayAgent`를 재구성하면 섹션이 재조립돼 인덱스가 fresh 반영된다. 기본 `FileMemoryStore`는 단일 프로세스/로컬 편의 — 진짜 분산 영속은 외부 Storage 기반 `MemoryStore`를 주입한다.

## ⑥ 비목표

Sonnet prefetch 랭킹 · 백그라운드 포크 추출 · 팀 메모리/시크릿 스캔/scope 태그 제외. (인덱스는 `step()`마다 `build_memory_section`이 재조립한다 — 캐시 없음.) `search`는 기본 도구가 아니며 커스텀 store가 `tools()`로 노출할 수 있다.
