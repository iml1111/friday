# 설계 스펙: 영속 메모리(Memory) — always-on 빌트인 + 교체 가능

- **작성일**: 2026-06-01
- **대상**: `friday_agent` (분산 에이전트 루프 SDK)
- **상태**: 설계 승인됨 · 미구현
- **선행/출처**: [`docs/proposals/2026-06-01-friday-memory-design.md`](../../proposals/2026-06-01-friday-memory-design.md) (제안서). 본 스펙은 그 제안서를 권위 기준으로 계승하되, **D6(엔진 무변경) → 엔진 통합 always-on**으로 뒤집는다(아래 §2). 나머지 결정(D1·D2·D3·D4·D5·D7)은 계승한다.
- **선례**: TodoWrite 도구·가이던스 빌트인화([`docs/proposals/2026-06-01-builtin-todo-injection-design.md`](../../proposals/2026-06-01-builtin-todo-injection-design.md))와 동일 위상 — SDK가 항상-켜진 능력을 엔진 경계에서 조립한다.

---

## 1. 목표 / 비목표

### 목표
- **세션 경계를 넘는 개인화**: user/feedback/project/reference 메모리를 새 세션 시작 시 인덱스로 주입해 더 개인화된 루프 동작 제공.
- **always-on 빌트인**: 메모리 능력(도구 + 가이던스 + 기본 store)을 호출자 배선 없이 **항상** 켠다. 호출자가 아무것도 안 해도 에이전트는 메모리를 쓸 수 있다.
- **단일 인터페이스로 교체 가능**: `MemoryStore` 하나가 **영속 백엔드 + 그 도구 표면**을 모두 소유한다. 호출자가 자신의 `MemoryStore`를 주입하면 기본 store와 기본 도구가 **통째로** 대체된다.
- **엔진 통합, 루프 무변경**: `FridayAgent`(엔진 경계)만 바꾼다. `run_one_turn`·`assemble_system_prompt`·`LoopState`·`Tool` ABC·orchestrator는 변경 0 — 코어 루프는 메모리·도구 이름을 모른 채 유지한다.
- **분산 정합**: 메모리(Store)와 대화(LoopState)를 분리한다. 분산 재개 시 Store는 컨테이너마다 재주입(provider/tools와 동일)되고 `LoopState` 직렬화는 불변.

### 비목표 (YAGNI — 제안서 §7 계승)
- **Sonnet prefetch 랭킹**(CC 메커니즘 ②) 제외. index-only 주입 + on-demand read로 충분. (향후 `MemoryStore.search()` 확장점.)
- **백그라운드 포크 추출**(CC 메커니즘 ③) 제외. friday엔 상주 프로세스 없음 — 인라인 자기주도 저장으로 대체.
- **팀 메모리·scope 태그·시크릿 스캔** 제외. 단일 store 전제; 멀티테넌트 네임스페이싱은 외부 store 오버라이드 책임.
- **매 턴 인덱스 갱신** 제외. 인덱스는 **세션 시작 스냅샷**(§5, D7).
- **`search` 기본 도구 아님**. 기본 도구는 save/read/delete 3종. 커스텀 store가 `tools()` 오버라이드로 search를 노출할 수 있다.
- **세션/체크포인트 영속화**는 본 설계와 별개(기존 `LoopState` serde가 담당).

---

## 2. 핵심 설계 결정

| # | 결정 | 근거 |
|---|---|---|
| **D-INTEGRATION** | **always-on 빌트인 + 엔진 통합** (제안서 D6의 "엔진 무변경"을 뒤집음) | 사용자 결정. TodoWrite 빌트인 선례와 동일 위상 — 능력을 엔진 경계에서 조립. 단, 코어 *루프*는 여전히 메모리를 모른다(주입은 `FridayAgent`에서만). |
| **D-SEAM** | **`MemoryStore` 한 클래스가 영속 + `tools()`를 모두 소유** | "스토어+도구를 하나의 인터페이스 기반 클래스로 묶어 주입" 요구. 백엔드만 교체=영속 메서드만 구현+기본 `tools()` 상속; 도구까지 교체=`tools()` 오버라이드. |
| **D-NAME** | 인터페이스 이름 = **`MemoryStore`** | "Provider"는 이 코드베이스에서 LLM completion 백엔드를 뜻해 혼동. "Store"가 영속을 명확히 표현. CLAUDE.md line 76의 `MemoryProvider` 언급을 이 이름·위치로 정정. |
| **D-DEFAULT** | 기본 store = **`FileMemoryStore("FRIDAY_MEMORY.md")`** (lazy) | 로컬/데모에서 "메모리가 실제로 저장되는 모습"을 즉시 봄. lazy라 생성만으론 파일 미생성. **분산 영속은 외부 Storage 기반 store 주입**(기본은 단일 프로세스/개발용). |
| **D-TOOLS** | 기본 도구 = **save / read / delete** | 인덱스가 전체 목록을 보여줘 작은 셋엔 검색 불요(on-demand read). delete로 틀린/낡은 메모리 제거. |
| D1 | 쓰기 = **에이전트 인라인 자기주도 저장**(전용 도구) | 제안서 계승. |
| D2 | 읽기 = **인덱스 주입 + on-demand 본문 read** | 제안서 계승. |
| D4 | 인덱스 = **store 메타데이터에서 자동 생성**(`load_index()`) | 제안서 계승(1단계 저장). |
| D5 | 내용 모델 = **CC 4-타입 + Why/How + what-NOT-to-save + staleness** | 제안서 계승. |
| D7 | 인덱스 주입 = **세션 시작 1회 스냅샷**(인스턴스 수명 캐시) | 제안서 계승. `system_prompt`가 세션-정적; 분산 재개 시 재구성마다 fresh. |

---

## 3. 모듈 구조 — `friday_agent/memory/` (신규)

```
friday_agent/memory/
  __init__.py  공개 심볼 재노출(MemoryStore·FileMemoryStore·InMemoryStore·MemoryEntry·
               IndexEntry·MemoryType·MemorySave·MemoryRead·MemoryDelete·build_memory_section)
  store.py     MemoryType · MemoryEntry · IndexEntry · MemoryStore(ABC) ·
               FileMemoryStore · InMemoryStore
  tool.py      MemorySaveInput/ReadInput/DeleteInput · MemorySave · MemoryRead · MemoryDelete
  prompt.py    MEMORY_INSTRUCTIONS · render_index(entries) · build_memory_section(store)
```

| 부품 | 책임 | 의존 |
|------|------|------|
| `MemoryStore` (ABC) | save/read/delete/load_index 계약 + `tools()` 기본 구현 | `Tool`(지연 import) |
| `FileMemoryStore` | `FRIDAY_MEMORY.md` 파싱·기록 (기본 백엔드, lazy) | `MemoryStore` |
| `InMemoryStore` | dict 기반 (테스트 더블 / 단일 프로세스 저비용) | `MemoryStore` |
| `MemorySave/Read/Delete` | 에이전트 표면 — store를 감싼 얇은 도구 | `MemoryStore`, `Tool` |
| `build_memory_section` | 정적 지침 + 자동 인덱스 → 주입 텍스트(async) | `MemoryStore` |

---

## 4. 데이터 모델 — `memory/store.py`

```python
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from friday_agent.tools.base import Tool


class MemoryType(str, Enum):            # CC 4-타입
    user = "user"
    feedback = "feedback"
    project = "project"
    reference = "reference"


@dataclass
class MemoryEntry:                      # 본문 포함 (read/save가 다룸)
    name: str                           # 안정적 식별자(kebab-case), upsert 키
    description: str                    # 한 줄 — 인덱스에 노출, relevance 판단용
    type: MemoryType
    body: str
    updated_at: float | None = None     # freshness("N days ago")용, 선택 (epoch sec)


@dataclass
class IndexEntry:                       # 메타데이터만 (주입용 — 본문 제외)
    name: str
    description: str
    type: MemoryType
    updated_at: float | None = None
```

---

## 5. `MemoryStore` ABC — 단일 주입 seam

```python
class MemoryStore(ABC):
    """영속 백엔드 + 그 도구 표면을 함께 소유하는 단일 주입 인터페이스.

    백엔드만 교체: save/read/delete/load_index만 구현(기본 tools() 상속).
    도구까지 교체: tools()를 오버라이드.
    """

    # --- 영속 (추상, 반드시 구현) ---
    @abstractmethod
    async def save(self, entry: MemoryEntry) -> None: ...        # name 기준 upsert(중복=갱신)
    @abstractmethod
    async def read(self, name: str) -> MemoryEntry | None: ...   # 본문 on-demand
    @abstractmethod
    async def delete(self, name: str) -> None: ...
    @abstractmethod
    async def load_index(self) -> list[IndexEntry]: ...          # 메타데이터 파생 TOC(본문 미로드)

    # --- 도구 표면 (기본 구현; 오버라이드로 도구 세트 교체) ---
    def tools(self) -> list["Tool"]:
        """이 store를 에이전트에 노출하는 도구. 기본 = self를 감싸는 save/read/delete."""
        from friday_agent.memory.tool import MemorySave, MemoryRead, MemoryDelete
        return [MemorySave(self), MemoryRead(self), MemoryDelete(self)]
```

- **async**: `Tool.call`·`LLMProvider.complete`와 동일 비동기 계약. 외부 Storage(네트워크)가 자연스럽게 들어온다.
- **upsert by name**: "중복 메모리 쓰지 말 것 — 기존 것 갱신"을 store 레벨에서 강제.
- **load_index ≠ read**: 인덱스는 메타데이터만(주입 비용 경계). 외부 store가 인덱스를 서버측에서 싸게 만들 수 있게 분리.
- **`tools()` 지연 import**: `store.py`↔`tool.py` 순환 import 회피.

### 5.1 기본 구현 `FileMemoryStore(path="FRIDAY_MEMORY.md")`

단일 섹션 파일. 개발자가 한 파일만 열면 전체 메모리를 본다.

```markdown
# FRIDAY_MEMORY.md
<!-- Friday agent memory. Auto-managed; 손으로 읽고 고쳐도 됨 -->

## [user] user-role
description: 분산 백엔드 엔지니어, Go 10년차
updated: 2026-06-01T10:00:00
---
Go 10년차, 이 레포 프론트는 처음. 프론트 설명은 백엔드 유비로.

## [feedback] testing-policy
description: 통합테스트는 실제 DB 사용, mock 금지
---
규칙: 통합테스트에서 DB를 mock하지 말 것.
**Why:** 지난 분기 mock 통과 후 prod 마이그레이션 실패.
**How to apply:** 이 영역 테스트 작성 시.
```

- **파싱 규칙**: `## [type] name`이 엔트리 시작 → 선택적 `description:` 줄 → 선택적 `updated: <iso>` 줄 → `---` → 다음 `## ` 또는 EOF까지 본문.
- **lazy**: 파일 부재 시 `load_index()`/`read()`는 빈 결과/`None` 반환(파일 미생성). 파일은 **첫 `save()`에서 생성**. → `FridayAgent` 생성만으론 디스크 IO 0.
- **`save()`**: `updated_at`을 `time.time()`으로 설정(미설정 시), 같은 `name` 섹션을 교체(upsert), 없으면 추가. 파일 전체 재기록.
- **`updated`(iso) ↔ `updated_at`(epoch)**: 파일엔 사람이 읽는 ISO로, `MemoryEntry`엔 epoch float로. 직렬화/역직렬화 시 변환. `updated` 줄 없으면 `updated_at=None`(freshness caveat 생략).
- **백엔드 무관 동작**: 파일이 본문을 다 갖고 있어도 store API는 **인덱스만 주입 + 본문 on-demand** 계약을 유지(외부 Storage 오버라이드 시 동작 동일).

### 5.2 `InMemoryStore`

`dict[str, MemoryEntry]` 기반. 테스트 더블 + 단일 프로세스 저비용 옵션. 같은 `MemoryStore` 인터페이스(기본 `tools()` 상속). 비영속.

---

## 6. 엔진 통합 — `friday_agent/core/engine.py` (변경 표면)

> `engine.py`만 바뀐다. `core/loop.py`·`tools/orchestrator.py`·`api/prompts.py`·`core/state.py`·`tools/base.py`는 **byte-unchanged**.

### 6.1 `__init__` — 기본 store + 도구 등록 + 충돌 검증

```python
# 새 인자 (max_concurrency 뒤)
memory: MemoryStore | None = None,
...
self._memory = memory if memory is not None else FileMemoryStore()
caller_tools = tools if tools is not None else []
builtins = builtin_tools()                  # TodoWrite
memory_tools = self._memory.tools()         # save/read/delete (또는 커스텀)
assembled = [*caller_tools, *builtins, *memory_tools]

# 조립된 전체 도구 목록의 이름이 유일한지 검증(중복 = ValueError).
# 중복 도구 이름을 LLM API가 거부하므로, 조용한 dedupe 대신 명시적 거부로 정합성을 지킨다.
seen, dups = set(), []
for t in assembled:
    if t.name in seen:
        dups.append(t.name)
    seen.add(t.name)
if dups:
    raise ValueError(
        f"FridayAgent: duplicate tool names {sorted(set(dups))}. "
        f"TodoWrite and the active MemoryStore's tools are SDK-managed and always "
        f"registered; remove the colliding tool(s) or override the store's tools()."
    )

self._provider = provider
self._tools = assembled
self._system_prompt = system_prompt
self._config = config
self._max_concurrency = max_concurrency
self._memory_section: str | None = None      # 세션-시작 스냅샷 캐시(lazy, async라 __init__에서 못 만듦)
```

- 기존 TodoWrite 전용 충돌 검사(`clash`)를 **전체 도구 유일성 검사로 일반화**한다(caller↔builtin, caller↔memory, memory↔builtin 모두 포괄).

### 6.2 `step()` — 메모리 섹션 async 조립 + 프롬프트 접합

```python
async def step(self, state: LoopState) -> AsyncGenerator[...]:
    if self._memory_section is None:                                  # 1회(인스턴스 수명)
        self._memory_section = await build_memory_section(self._memory)
    effective_prompt = (
        f"{self._system_prompt}\n\n{self._memory_section}"
        if self._system_prompt else self._memory_section
    )
    tool_schemas = [tool.get_tool_schema() for tool in self._tools]
    async for item in run_one_turn(
        provider=self._provider,
        tools=self._tools,
        tool_schemas=tool_schemas,
        state=state,
        system_prompt=effective_prompt,        # base + 메모리 섹션
        config=self._config,
        max_concurrency=self._max_concurrency,
    ):
        yield item
```

- 최종 시스템 프롬프트 순서: **base → 메모리 섹션 → GENERAL_AGENT_GUIDANCE → TODO_GUIDANCE** (`run_one_turn` 내부 `assemble_system_prompt`가 뒤의 둘을 덧붙임).
- `run_one_turn` 시그니처/본문 불변 — 기존 `system_prompt: str` 인자에 접합된 문자열을 넘길 뿐.

### 6.3 `compact()` — 메모리 섹션 미주입

`compact()`는 `compact_conversation()`을 직접 호출하며 `step()`의 프롬프트 경로를 타지 않는다 → **메모리 섹션·인덱스가 요약에 새지 않는다**(GENERAL/TODO 가이던스가 요약에 안 새는 것과 동일 철학). `compact()`는 변경하지 않는다(검증만).

---

## 7. 주입 텍스트 — `memory/prompt.py`

```python
MEMORY_INSTRUCTIONS: str = """# Memory
You have a persistent memory that survives across sessions, accumulated over time.
Use the memory tools to save and recall typed facts.

## Types of memory
 - user: who the user is (role, preferences, expertise).
 - feedback: how the user wants you to work (corrections, confirmed approaches). Include the why.
 - project: ongoing work/goals/constraints not derivable from code or git.
 - reference: pointers to external resources (URLs, dashboards, tickets).

## When to save
Save when you learn a durable fact in one of the four types. For feedback/project,
structure the body as the rule/fact, then **Why:** and **How to apply:** lines.
Reuse an existing `name` to UPDATE rather than duplicate.

## What NOT to save
Do not save what is derivable (code structure, architecture, git history), one-off
debugging fixes, or ephemeral conversation/run state.

## When to access
Read a memory when it is relevant or the user asks. If a memory names a file,
function, or flag, verify it still exists before relying on it — a memory saying X
does not guarantee X exists now. If the user says to ignore memory, act as if empty.

The memory index below is a session-start snapshot; memories you save this session
appear in your tool_result immediately but in the index only next session."""


def render_index(entries: list[IndexEntry]) -> str:
    if not entries:
        return ("## Current memory index\n"
                "Your memory is empty. Save memories as you learn about the user, "
                "their feedback, and the project.")
    lines = "\n".join(f"- [{e.type.value}] {e.name} — {e.description}" for e in entries)
    return f"## Current memory index\n{lines}"


async def build_memory_section(store: MemoryStore) -> str:
    index = await store.load_index()
    return f"{MEMORY_INSTRUCTIONS}\n\n{render_index(index)}"
```

- **정적 지침 먼저, 동적 인덱스 끝** — 캐싱 시 정적 prefix 보존.
- `build_memory_section`은 항상 non-empty(빈 store도 "empty" 안내) → 항상 주입.

---

## 8. 도구 — `memory/tool.py`

```python
class MemorySaveInput(BaseModel):
    name: str = Field(description="Stable kebab-case id; reuse the same name to UPDATE an existing memory")
    description: str = Field(description="One-line summary shown in the memory index; used to judge relevance later — be specific")
    type: MemoryType = Field(description="user | feedback | project | reference")
    body: str = Field(description="Memory content. For feedback/project, structure as the rule/fact, then **Why:** and **How to apply:** lines")

class MemoryReadInput(BaseModel):
    name: str = Field(description="The memory's stable name (as shown in the index)")

class MemoryDeleteInput(BaseModel):
    name: str = Field(description="The memory's stable name to delete")
```

| Tool | name | input | 동작 | concurrency_safe |
|------|------|-------|------|---|
| `MemorySave` | `memory_save` | name, description, type(enum), body | `store.save(MemoryEntry(...))` → `ToolResult(data="Saved <type> memory '<name>'.")` | `False`(변이) |
| `MemoryRead` | `memory_read` | name | `store.read(name)` → 본문; `updated_at`이 1일(86400s) 초과면 staleness caveat 부착. 없으면 `is_error` 결과 | `True`(read-only) |
| `MemoryDelete` | `memory_delete` | name | `store.delete(name)` → `ToolResult(data="Deleted memory '<name>'.")` | `False`(변이) |

- 셋 다 일반 `ToolResult(data=...)`만 반환 — **`state_effect` 없음**(메모리는 LoopState가 아니라 Store에 삶).
- 도구 description = `__class__.__doc__`(LLM 전달) — "무엇을·언제"를 충실히 작성.
- **staleness caveat**(`MemoryRead`): `updated_at`이 1일 초과 시 본문 뒤에 한 줄 — `(This memory was written N days ago; verify any named file/function/flag still exists before relying on it.)`. 현재 시각은 호출 시 `time.time()`.
- 분리형 3-도구(단일 `action` 디스크리미네이터 대신): 액션별 풍부한 스키마가 LLM에 더 명확.

---

## 9. 데이터 흐름

```
[세션 시작 — FridayAgent(provider, ..., memory=None|store)]
  self._memory = store or FileMemoryStore()
  self._tools  = caller_tools + [TodoWrite()] + self._memory.tools()   # 충돌 시 ValueError
  self._memory_section = None                                          # lazy

[턴 루프 — caller가 step()으로 구동]
  step() 첫 호출: self._memory_section = await build_memory_section(self._memory)  # 스냅샷
  effective = base + 메모리 섹션 → run_one_turn(system_prompt=effective)
    └─ assemble_system_prompt: + GENERAL + TODO
    └─ 모델이 인덱스를 봄
         ├─ memory_read(name) → store.read() → 본문(+staleness caveat)
         └─ memory_save({...}) → store.save() (upsert) → 파일/Store 갱신
                                 → tool_result(모델 즉시 인지)
  ContextOverflow → caller가 engine.compact() → 대화는 요약 1개로 접힘
                    (메모리 섹션 미주입; Store는 독립 보존 — 영향 없음)

[다음 세션 / 컨테이너 B]
  대화(LoopState)는 caller가 외부 Storage에 직렬화(기존 메커니즘; 메모리 본문 미포함)
  메모리(Store)는 FRIDAY_MEMORY.md / 외부 Storage에 영속
  → FridayAgent 재구성 → build_memory_section 재조립 → 누적 메모리가 인덱스로 fresh 주입
```

---

## 10. 불변식 보존 & 분산 안전

- **코어 루프 무변경**: `run_one_turn`/`orchestrator`/`assemble_system_prompt`/`LoopState`/`Tool` ABC 불변. 루프는 메모리·도구 이름을 모른다(주입은 `FridayAgent`에서만).
- **par-critical 정합**: 메모리 도구는 일반 도구 경로(orchestrator)를 타므로 `tool_use↔tool_result` 정합에 새 위험 0.
- **분산 재개**: `MemoryStore`는 `LoopState`에 직렬화되지 않음(serde 불변, provider/tools와 동일 컨테이너-로컬 재주입). 컨테이너 B가 store로 재구성 + 섹션 재조립 → 인덱스 fresh.
- **compaction 보존**: `compact()`가 대화를 요약 1개로 접어도 메모리는 Store에 독립 보존. 메모리는 ① 세션 내 compaction과 ② 세션 경계를 둘 다 넘는 연속성을 제공.
- **세션-시작 스냅샷 함의**: 인스턴스 수명 캐시라, 같은 프로세스에서 여러 step에 걸쳐 저장한 메모리는 그 인스턴스의 인덱스엔 안 뜬다(다음 재구성에서 fresh). 단 에이전트는 `tool_result`로 즉시 인지하므로 세션 내 일관성 보존(제안서 D7).
- **always-on의 무해성 한계**: 메모리는 이제 항상 켜져 있어 "안 쓰면 no-op"은 아니다. 단 기본 `FileMemoryStore`가 lazy라 **사용 전 디스크 IO 0**이고, 빈 store는 "empty" 인덱스만 주입한다.

---

## 11. 하위 정리 & 문서 동기화

### 11.1 `scripts/run_agent.py` (필수)
- 메모리 빌트인 반영: 별도 배선 불필요. 배너/docstring에 "메모리 always-on(기본 `FileMemoryStore`→`FRIDAY_MEMORY.md`, 세션 경계 넘어 영속)" 명시.
- 선택: `_print_new_messages`에서 `memory_save`/`memory_read`/`memory_delete` tool_use를 간결히 표시(현 TodoWrite 특수 처리와 동일 스타일).

### 11.2 문서 (SSOT 동기화)
- **신규** `docs/architecture/08-memory.md` — 메모리 서브시스템 章(모듈·인터페이스·주입·분산).
- `docs/architecture/00-overview.md` — 모듈 지도에 `memory/` 1줄 추가, 읽는 순서/스코프 반영.
- `docs/architecture/02-tool-orchestration.md` §⑥ 빌트인 노트 — TodoWrite 외에 "활성 `MemoryStore`의 `tools()`도 항상 등록(이름 충돌 시 `ValueError`)" 추가.
- **CLAUDE.md**:
  - line 76 `MemoryProvider`(옵션·NoOp) → `MemoryStore`(always-on·교체가능)로 정정.
  - "LLM 추상화 경계 = 3개 인터페이스" 목록에서 메모리를 분리해 **독립 메모리 서브시스템**으로 기술.
  - 구현 스코프 헌장에 "메모리(영속·인덱스 주입·자기주도 저장)" 포함, 비목표(prefetch 랭킹·백그라운드 추출·팀 메모리) 반영.
  - 패키지 구조 줄에 `memory/`(store·tool·prompt) 추가.

---

## 12. 테스트 전략 (키 불필요 · fake provider)

### 단위
- **`FileMemoryStore`**: save→read 왕복; `load_index()`가 본문 없이 메타데이터만; upsert(같은 name 재저장=갱신); delete; `updated` 유무에 따른 `updated_at`; **파일 부재 시 load_index/read = 빈 결과(파일 미생성)**; 첫 save에서 파일 생성; `updated`(iso)↔`updated_at`(epoch) 변환.
- **`InMemoryStore`**: 같은 계약 왕복; 비영속.
- **`MemoryStore.tools()`**: 기본 = `memory_save/read/delete` 인스턴스; 서브클래스가 오버라이드하면 그 세트 반환.
- **`MemorySave/Read/Delete.call`**: 스키마 검증; store 위임; `MemoryRead`의 staleness caveat(>1일) 부착·미부착(≤1일/None); 없는 name read = `is_error` 결과.
- **`build_memory_section`/`render_index`**: 빈 store → "empty" 문구; 항목 있으면 `[type] name — description` 라인; 정적 지침이 동적 인덱스보다 앞.

### 통합 (fake provider)
- 기본(주입 안 함): `step()`의 `tool_schemas`에 `memory_save/read/delete` 포함; `received_system_prompts`에 `MEMORY_INSTRUCTIONS` 포함.
- 모델이 `memory_save` tool_use 방출 → 호출 정상 처리 → 기본 `FileMemoryStore`/`InMemoryStore`에 반영(다음 `build_memory_section` 인덱스에 출현).
- 모델이 `memory_read` → 본문 반환; 오래된 항목엔 caveat.
- **교체**: 커스텀 `MemoryStore`(오버라이드 `tools()` — 예: search 포함) 주입 → 기본 3종 대신 그 도구들이 등록됨.
- **충돌**: caller `tools=`에 `memory_save`(또는 `TodoWrite`) 이름 도구 주입 → `__init__`에서 `ValueError`(메시지에 도구 이름 포함).
- **compact 청결**: `compact()` 경로의 프롬프트에 `MEMORY_INSTRUCTIONS`·인덱스 미포함.

### 분산
- 세션 A 저장 → Store(파일/메모리)에 영속 → "컨테이너 B"에서 `FridayAgent` 재구성 + `build_memory_section` 재조립 → 인덱스 fresh 반영. **`LoopState.to_dict()`에 메모리 본문이 새지 않음**(serde에 memory 키 없음 확인).

### 회귀
- 기존 전체 테스트 그린 유지(특히 충돌 검사 일반화가 TodoWrite 케이스를 깨지 않음, compact 청결 테스트 유지).
