# 설계 제안서: 영속 메모리(Memory) 접목

- **작성일**: 2026-06-01
- **대상**: `friday_agent` (분산 에이전트 루프 SDK)
- **상태**: 설계 승인됨 · 미구현 · 현행 코드(`FridayAgent` 생성자 / `assemble_system_prompt` 1-arg / `Tool` ABC) 기준 검증됨 (본 문서는 제안서이며 구현 계획·코드는 포함하지 않음)
- **출처**: Claude Code의 파일시스템 기반 메모리(`memdir` / `MEMORY.md` / 4-타입 택소노미)를 friday의 순수-도구·caller-driven·분산 구조에 맞춰 이식

---

## 1. 배경 & 동기

Claude Code의 메모리는 **세션 경계를 넘어 일부 컨텍스트를 장기화**하는 서브시스템이다. 세 메커니즘으로 구성된다:

1. **세션 시작 인덱스 주입** — `MEMORY.md`(메모리 인덱스)와 행동 지침을 시스템 프롬프트에 주입.
2. **세션 중 관련 메모리 prefetch** — Sonnet side-query가 사용자 쿼리에 맞는 메모리 파일 최대 5개를 골라 `<system-reminder>`로 주입.
3. **턴 종료 시 백그라운드 추출** — 포크된 에이전트가 직전 대화에서 4-타입(user/feedback/project/reference) fact를 증류해 파일로 저장.

friday는 현재 이 개념이 **전혀 없다**(확인: `friday_agent/` 전역에 memory 추상화·recall·persist 없음). 유일한 지속 메커니즘은 compaction(손실성 요약)뿐이다. 또한 friday는 Claude Code와 구조가 근본적으로 다르다:

- **caller-driven `step()`**: 상주 백그라운드 프로세스가 없다 → CC의 "턴 종료 포크 추출"을 그대로 옮길 수 없다.
- **세션-정적 컨텍스트**: `system_prompt`/`tools`는 `FridayAgent.__init__`의 인자로 고정된다(`core/engine.py:45-67`). 매 턴 재주입 채널이 아니다.
- **외부 Storage 전제**: 모든 대화 세션과 메모리는 외부 Storage에 산다. Storage 통신은 **개발자가 제공하는 Tool**로 한정된다(라이브러리는 파일시스템을 가정하지 않는다).
- **분산 직렬화**: 대화는 `LoopState.to_dict/from_dict`로 컨테이너 간 이송되지만, provider/tools는 컨테이너-로컬 재주입이다.

따라서 목표는 "CC 메모리의 **본질**(타입화된 fact의 장기화 + 인덱스 주입 + 자기주도 저장)을 friday의 제약(순수 도구·caller-driven·세션-정적·외부 Storage) 위에 **엔진 변경 없이** 얹는 것"이다.

---

## 2. 목표 / 비목표

### 목표
- **세션 경계를 넘는 개인화**: 과거 세션에서 축적한 user/feedback/project/reference 메모리를 새 세션 시작 시 컨텍스트로 주입해 더 개인화된 루프 동작 제공.
- **외부 Storage 추상화**: 메모리 영속을 단일 인터페이스(`MemoryStore`)로 추상화하고, 개발자가 자신의 Storage로 오버라이드. 기본 구현은 **사람이 열어볼 수 있는 파일**(`FRIDAY_MEMORY.md`)로 제공해 "메모리가 실제로 어떻게 저장되는지" 즉시 인지 가능하게.
- **자기주도 저장**: 에이전트가 일반 턴 중 전용 도구로 메모리를 직접 저장(별도 추출 프로세스 불요).
- **엔진 무변경**: `FridayAgent`/`run_one_turn`/`LoopState` 코어를 건드리지 않고, 메모리를 **조립 가능한 라이브러리 부품 + caller 배선**으로만 구현.
- **분산 정합**: 메모리(Store)와 대화(LoopState)를 분리해, 분산 재개 시 Store는 컨테이너마다 재주입(provider/tools와 동일 취급)되고 LoopState 직렬화는 불변.

### 비목표 (YAGNI)
- **Sonnet prefetch 랭킹**(CC 메커니즘 ②)은 넣지 않는다. index-only 주입 + 에이전트 on-demand read로 충분. (향후 `MemoryStore.search()` 확장점으로 남김.)
- **백그라운드 포크 추출**(CC 메커니즘 ③)은 넣지 않는다. friday엔 상주 프로세스가 없다 — 인라인 자기주도 저장으로 대체. (향후 caller-driven 추출 패스로 확장 가능.)
- **팀 메모리·scope 태그·시크릿 스캔**은 범위 밖. 단일 store를 전제하고, 멀티테넌트 네임스페이싱은 외부 store 오버라이드의 책임으로 둔다.
- **세션/체크포인트 영속화**는 본 설계와 별개다. 기존 `LoopState` 직렬화(caller 소유)가 담당한다. 본 설계는 메모리 store만 다룬다.
- **매 턴 인덱스 갱신**은 하지 않는다. `system_prompt`가 세션-정적이므로 인덱스는 **세션 시작 스냅샷**이다(아래 §4.5).

---

## 3. 핵심 설계 결정 (요약)

| # | 결정 | 근거 |
|---|---|---|
| D1 | 쓰기 = **에이전트 인라인 자기주도 저장** (전용 도구 호출) | friday엔 상주 백그라운드 없음; CC의 direct-write 경로에 해당. 트레이드오프(미호출 시 미저장)는 강한 `when_to_save` 지침으로 완화 |
| D2 | 읽기 = **인덱스 주입 + on-demand 본문 read** | CC `MEMORY.md` 모델. 토큰 비용 경계적, 랭킹 side-query 불요 |
| D3 | 영속 = **`MemoryStore` ABC** (단일 오버라이드 seam), 기본 **`FileMemoryStore`→`FRIDAY_MEMORY.md`** | LLMProvider 철학(작동하는 기본값 + 주입 교체); 파일 기본값은 "저장 형태 가시화"용 레퍼런스 |
| D4 | 인덱스 = **store 메타데이터에서 자동 생성** (`load_index()`) | CC의 수동 2단계 저장(본문+인덱스 포인터)을 **1단계로 축약**; 인덱스↔본문 드리프트 제거 |
| D5 | 내용 모델 = **CC 4-타입 + 품질 가드레일 이식** | eval-검증된 택소노미(user/feedback/project/reference) + Why/How 본문 구조 + what-NOT-to-save + staleness caveat |
| D6 | 통합 = **caller 소유 + 순수 주입** (엔진 무변경) | friday 철학(caller가 step/compact를 몰듯 메모리 주입도 caller가 몬다); 메모리 미배선 시 코어 영향 0 |
| D7 | 주입 시점 = **세션 시작(생성) 시 1회 스냅샷** | `system_prompt`가 `FridayAgent.__init__` 인자로 세션-정적; "loop 시작시 주입" 요구와 일치 |

---

## 4. 설계 상세

### 4.1 모듈 경계 — `friday_agent/memory/` (신규)

엔진과 분리된 신규 모듈. 메모리를 안 쓰면 없는 것과 동일(코어 영향 0).

```
friday_agent/memory/
  store.py     MemoryStore (ABC) · MemoryEntry · IndexEntry · MemoryType
               FileMemoryStore(기본) · InMemoryStore(테스트 더블)
  tool.py      MemorySave · MemoryRead · MemoryDelete   (각 Tool ABC 구현)
  prompt.py    build_memory_section(store) · MEMORY_INSTRUCTIONS · render_index
```

| 부품 | 책임 | 의존 |
|------|------|------|
| `MemoryStore` (ABC) | save/read/load_index/delete/(search) 계약 | 없음 (순수 인터페이스) |
| `FileMemoryStore` | `FRIDAY_MEMORY.md` 파싱·기록 (기본 백엔드) | `MemoryStore` |
| `MemorySave/Read/Delete` | 에이전트 표면 — store를 감싼 얇은 도구 | `MemoryStore`, `Tool` |
| `build_memory_section` | 정적 지침 + 자동 인덱스 → 주입 텍스트 | `MemoryStore` |

### 4.2 데이터 모델 — `memory/store.py`

```python
class MemoryType(str, Enum):           # CC 4-타입
    user = "user"; feedback = "feedback"; project = "project"; reference = "reference"

@dataclass
class MemoryEntry:                      # 본문 포함 (read/save가 다룸)
    name: str                          # 안정적 식별자(kebab-case), upsert 키
    description: str                   # 한 줄 — 인덱스에 노출, 미래 relevance 판단용
    type: MemoryType
    body: str
    updated_at: float | None = None    # freshness("N days ago")용, 선택

@dataclass
class IndexEntry:                      # 메타데이터만 (주입용 — 본문 제외)
    name: str
    description: str
    type: MemoryType
    updated_at: float | None = None
```

### 4.3 `MemoryStore` ABC — 단일 오버라이드 seam (async)

```python
class MemoryStore(ABC):
    @abstractmethod
    async def save(self, entry: MemoryEntry) -> None: ...      # name 기준 upsert(중복=갱신)
    @abstractmethod
    async def read(self, name: str) -> MemoryEntry | None: ... # 본문 on-demand
    @abstractmethod
    async def delete(self, name: str) -> None: ...
    @abstractmethod
    async def load_index(self) -> list[IndexEntry]: ...        # 메타데이터 파생 TOC(본문 미로드)
    async def search(self, query: str) -> list[IndexEntry]:    # 선택 — 기본 = description/name substring
        results = await self.load_index()
        q = query.lower()
        return [e for e in results if q in e.name.lower() or q in (e.description or "").lower()]
```

- **async**: `Tool.call`·`LLMProvider.complete`와 동일 비동기 계약. 외부 Storage(네트워크)가 자연스럽게 들어온다.
- **upsert by name**: "중복 메모리 쓰지 말 것 — 기존 것 갱신"(CC 규칙)을 store 레벨에서 강제.
- **load_index ≠ read**: 인덱스는 메타데이터만(주입 비용 경계). 외부 store가 인덱스를 서버측에서 싸게 만들 수 있게 분리.

### 4.4 기본 구현 `FileMemoryStore(path="FRIDAY_MEMORY.md")`

단일 섹션 파일. 개발자가 한 파일만 열면 전체 메모리를 본다. store가 섹션↔`MemoryEntry`를 매핑하고, `load_index()`는 섹션 헤더+`description`만 모은다.

```markdown
# FRIDAY_MEMORY.md
<!-- Friday agent memory. Auto-managed; 손으로 읽고 고쳐도 됨 -->

## [user] user_role
description: 분산 백엔드 엔지니어, Go 10년차
---
Go 10년차, 이 레포 프론트는 처음. 프론트 설명은 백엔드 유비로.

## [feedback] testing_policy
description: 통합테스트는 실제 DB 사용, mock 금지
---
규칙: 통합테스트에서 DB를 mock하지 말 것.
**Why:** 지난 분기 mock 통과 후 prod 마이그레이션 실패.
**How to apply:** 이 영역 테스트 작성 시.
```

- **파싱 규칙**: `## [type] name`이 엔트리 시작 → `description:` 줄 → `---` → 다음 `## ` 또는 EOF까지 본문. 선택적 `updated: <iso>` 메타 줄로 freshness(없으면 caveat 생략 — 위 포맷과 호환).
- **백엔드 무관 동작**: 파일이 본문을 다 갖고 있어도 store API는 **인덱스만 주입 + 본문 on-demand** 계약을 유지한다. 그래야 외부 Storage 오버라이드로 바뀌어도 에이전트 동작이 동일하다.
- `InMemoryStore`는 dict 기반 테스트 더블로만 잔존(단일 프로세스, 비영속).

### 4.5 Recall 주입 — `build_memory_section` + `system_prompt` 접합

`system_prompt`는 `FridayAgent.__init__` 인자로 **세션-정적**이다(`core/engine.py:49,65`). 따라서 메모리 인덱스는 **세션 시작(에이전트 생성) 시 1회** 조립해 `system_prompt`에 접합한다("loop 시작시 주입" 요구와 일치).

```python
async def build_memory_section(store: MemoryStore) -> str:
    index = await store.load_index()
    return f"{MEMORY_INSTRUCTIONS}\n\n{render_index(index)}"
```

반환 텍스트 구조 — **정적 지침 먼저, 동적 인덱스는 끝**(캐싱 시 정적 prefix 보존):

```
{MEMORY_INSTRUCTIONS}                  ← 정적, 매 세션 동일

## Current memory index                ← 동적, store.load_index()에서 자동 생성
- [user] user_role — 분산 백엔드 엔지니어, Go 10년차
- [feedback] testing_policy — 통합테스트 실제 DB, mock 금지
- [project] auth_rewrite — auth 교체는 컴플라이언스 요구
(비어있으면) "Your memory is empty. Save memories as you learn about the user, their feedback, and the project."
```

**caller 배선** (엔진 무변경 — 생성자 인자만):

```python
store = FileMemoryStore("FRIDAY_MEMORY.md")
section = await build_memory_section(store)              # 세션 시작 1회
agent = FridayAgent(
    provider=provider,
    tools=[MemorySave(store), MemoryRead(store), MemoryDelete(store), *other_tools],
    system_prompt=f"{base_prompt}\n\n{section}",          # 세션-정적 주입
    config=config,
)
# 이후 caller가 step()으로 턴 루프를 몬다: async for item in agent.step(state): ...
```

엔진의 `assemble_system_prompt(system_prompt)`가 뒤에 `GENERAL_AGENT_GUIDANCE`를 붙이므로 최종 순서는 `base → 메모리 섹션 → 일반 지침`.

> **세션-정적 함의**: 에이전트가 세션 중 새 메모리를 저장해도 인덱스는 다음 세션에야 반영된다. 단 에이전트는 방금 저장한 내용을 `tool_result`로 즉시 인지하므로 이번 세션 내 일관성은 보존된다. 분산 재개 시 컨테이너 B는 `FridayAgent`를 재구성하며 `build_memory_section`을 **다시** 조립하므로, 인덱스가 재개 시점의 Store 상태를 신선하게 반영한다(매 턴 갱신보다 단순하고, 재개마다 fresh).

### 4.6 `MEMORY_INSTRUCTIONS` — CC `memdir` 지침 이식 (1단계 저장로 축약)

| 섹션 | 내용 | friday 변경점 |
|------|------|--------------|
| 인트로 | "지속적 파일 메모리, 시간에 걸쳐 축적" | 동일 |
| **Types of memory** | 4-타입 + `when_to_save`/`how_to_use`/`body_structure`(Why·How) | `<scope>` 태그 제거(단일 store) |
| **What NOT to save** | 도출 가능한 것(코드·아키텍처·git)·디버깅 해법·휘발성 상태 금지 | 동일 |
| **How to save** | "`memory_save`를 name/description/type/body로 호출" | **CC 2단계 → 1단계** (인덱스 자동; 에이전트가 인덱스를 손대지 않음) |
| **When to access** | 관련 시·명시 요청 시·"ignore" 지시(빈 것처럼 행동) | "grep memory dir" → `memory_read`/`memory_search` |
| **Before recommending** | 파일/함수/플래그를 명명한 메모리는 추천 전 검증; "메모리가 X라 함 ≠ X가 지금 존재" | 동일 |

> CC의 `plan/tasks 대비` 섹션은 friday 코어에 plan/task 개념이 없어 **제외**. CC의 "Searching past context"(transcript `.jsonl` grep)는 세션이 외부 Storage(caller 소유)에 있어 **제외**.

### 4.7 `MemoryTool` 3종 — 에이전트 표면 (`memory/tool.py`)

같은 store를 감싸는 얇은 도구. 도구 description = `__class__.__doc__`(LLM 전달, `tools/base.py:60`)이므로 docstring을 "무엇을·언제" 충실히 쓴다.

```python
class MemorySaveInput(BaseModel):
    name: str = Field(description="Stable kebab-case id; reuse the same name to UPDATE an existing memory")
    description: str = Field(description="One-line summary shown in the memory index; used to judge relevance later — be specific")
    type: MemoryType = Field(description="user | feedback | project | reference")
    body: str = Field(description="Memory content. For feedback/project, structure as the rule/fact, then **Why:** and **How to apply:** lines")

class MemorySave(Tool):
    """Save or update a long-term memory that persists across sessions. Use when you
    learn something about the user, their feedback on how to work, project context not
    derivable from code, or a pointer to an external system. Reuse an existing `name`
    to update rather than duplicate. Do NOT save code/architecture/git facts or
    ephemeral conversation state."""
    name = "memory_save"
    def __init__(self, store: MemoryStore) -> None: self._store = store
    def input_schema(self) -> type[BaseModel]: return MemorySaveInput
    async def call(self, args: dict) -> ToolResult:
        inp = MemorySaveInput(**args)
        await self._store.save(MemoryEntry(inp.name, inp.description, inp.type, inp.body))
        return ToolResult(data=f"Saved {inp.type.value} memory '{inp.name}'.")
```

| Tool | input | 동작 |
|------|-------|------|
| `memory_save` | name, description, type(enum), body | `store.save()` — type enum·description 필수가 품질을 스키마로 유도 |
| `memory_read` | name | `store.read()` → 본문; `updated_at`이 1일 초과면 staleness caveat 부착(CC `memoryFreshnessNote` 이식) |
| `memory_delete` | name | `store.delete()` — "틀린/낡은 메모리 제거" 지원 |

- 일반 `ToolResult(data=...)`만 반환 — `state_effect` 불요(메모리는 LoopState가 아니라 Store에 산다).
- 단일 `action` 디스크리미네이터 도구 대신 **분리형 3-도구**: 액션별 풍부한 스키마가 LLM에 더 명확.
- `search`는 외부 store가 지원할 때만 `memory_search`로 노출하는 **선택** 도구.

---

## 5. 한 세션 데이터 흐름

```
[세션 시작]
  store = FileMemoryStore("FRIDAY_MEMORY.md")
  section = build_memory_section(store)            # 지침 + 자동 인덱스(스냅샷)
  agent = FridayAgent(tools=[memory_*, ...], system_prompt = base + section)

[턴 루프 — caller가 step()으로 구동]
  모델은 인덱스를 봄
    ├─ 관련 본문 필요 → tool_use(memory_read, name) → store.read() → 본문(+신선도 caveat)
    └─ 저장 가치 신호 포착 → tool_use(memory_save, {name,desc,type,body})
                              → store.save() (upsert) → FRIDAY_MEMORY.md 갱신
                              → tool_result (모델 즉시 인지) → LoopState에 기록
  ContextOverflow → caller가 engine.compact() → 대화는 요약 1개로 접힘
                    (메모리는 Store에 독립 보존 — 영향 없음)

[세션 종료 / 다음 세션]
  대화(LoopState)는 caller가 외부 Storage에 직렬화(기존 메커니즘)
  메모리(Store)는 FRIDAY_MEMORY.md / 외부 Storage에 영속
  → 다음 세션 시작 시 section 재조립 → 누적된 메모리가 인덱스로 주입
```

핵심: **대화(LoopState)와 장기 메모리(Store)는 별개 저장소**다. 메모리 도구의 `tool_result`는 LoopState에 남지만, 실제 메모리 본문은 Store에 산다.

---

## 6. 불변식 보존 & 분산 안전

- **엔진 무변경**: `FridayAgent`/`run_one_turn`/`LoopState`/`Tool` ABC 어느 것도 수정하지 않는다. 메모리는 신규 `memory/` 모듈 + caller 배선뿐.
- **tool_use ↔ tool_result 정합**: 메모리 도구는 일반 도구와 동일 경로(`orchestrator`)를 타므로 정합 불변식에 새 위험을 추가하지 않는다.
- **분산 재개**: Store는 `LoopState`에 직렬화되지 않는다(provider/tools와 동일 컨테이너-로컬 재주입). 컨테이너 B는 같은 외부 Storage를 가리키는 store로 `FridayAgent`를 재구성하고 `build_memory_section`을 다시 조립 → 인덱스가 재개 시점 상태를 신선 반영. LoopState 직렬화는 불변.
- **compaction 보존**: `engine.compact()`가 대화를 요약 1개로 접어도 메모리는 Store에 독립 보존. 메모리는 ① 세션 내 compaction과 ② 세션 경계를 **둘 다 넘는 연속성**을 제공한다(개인화의 실제 페이로프).
- **점진 도입·무해성**: 메모리 도구를 `tools`에 등록하지 않고 `section`을 주입하지 않으면 friday는 정확히 기존과 동일하게 동작(no-op). 메모리를 배선하되 `FRIDAY_MEMORY.md`가 아직 없으면 `FileMemoryStore`가 빈 인덱스를 반환하고 첫 `memory_save`에서 파일을 생성.
- **백엔드별 재개 특성**: `InMemoryStore`=단일 프로세스만, `FileMemoryStore`=파일이 공유 볼륨이면 생존, **외부 Storage 오버라이드 = 진짜 분산 답**.

---

## 7. 명시적 경계 (이번 설계 제외)

| 제외 항목 | 이유 | 향후 여지 |
|----------|------|----------|
| Sonnet prefetch 관련 메모리 랭킹 | index-only + on-demand 선택(D2) | `MemoryStore.search()` 확장점 |
| 백그라운드 포크 추출 | friday 상주 프로세스 없음(D1 인라인 write) | caller-driven 추출 패스 추가 가능 |
| 팀 메모리·시크릿 스캔·scope 태그 | 단일 store; 네임스페이싱은 store 책임 | 외부 store가 처리 |
| 세션/체크포인트 영속화 | 기존 `LoopState.to_dict` 메커니즘(caller 소유) | 메모리 설계와 별개 |
| 매 턴 인덱스 갱신 | `system_prompt` 세션-정적(D7) | 재개마다 재조립으로 충분 |

---

## 8. CC → friday 매핑 (추적성)

| Claude Code | friday | 성격 |
|---|---|---|
| `~/.claude/projects/<slug>/memory/` 파일시스템 | `MemoryStore` ABC (기본 `FileMemoryStore`→`FRIDAY_MEMORY.md`) | 추상화 |
| `MEMORY.md` 수동 인덱스 (2단계 저장) | store 메타데이터 자동 인덱스 (1단계) | **개선** |
| 4-타입 + Why/How + what-NOT-to-save | `MEMORY_INSTRUCTIONS` 이식 | 동일(scope 태그 제거) |
| harness의 시스템 프롬프트 주입 | `build_memory_section` (caller가 `system_prompt`에 접합) | harness → caller |
| Sonnet prefetch 랭킹 | index-only + on-demand read | **제외** |
| 턴 종료 포크 추출 | 인라인 `memory_save` | **제외/대체** |
| 생성 도구(Read/Edit/Write/Grep)로 메모리 접근 | 전용 `memory_save`/`read`/`delete` 도구 | FS 비의존 |
| `memoryAge`/`memoryFreshnessNote` 신선도 | `updated_at` + read-time caveat | 이식 |
| 팀 메모리/시크릿 스캔 | (제외) | 단일 store |

---

## 9. 변경 표면 (touch map)

| 파일 | 변경 | 성격 |
|---|---|---|
| `friday_agent/memory/store.py` | **신규** — `MemoryStore`/`MemoryEntry`/`IndexEntry`/`MemoryType`/`FileMemoryStore`/`InMemoryStore` | 추상화 + 기본 백엔드 |
| `friday_agent/memory/tool.py` | **신규** — `MemorySave`/`MemoryRead`/`MemoryDelete`(+선택 `MemorySearch`) | 에이전트 표면 |
| `friday_agent/memory/prompt.py` | **신규** — `MEMORY_INSTRUCTIONS`·`build_memory_section`·`render_index` | 주입 텍스트 |
| `friday_agent/core/*` | **변경 없음** | 엔진 무변경(설계 핵심) |
| `scripts/` (예시) | caller 배선 예시(생성자에 memory 도구 + section 주입) | 통합 데모 |
| `tests/` | 단위 + 통합(분산 재개) | 검증 |
| `docs/architecture/` | 메모리 모듈을 설명하는 신규 章(또는 00-overview 모듈 맵 1줄) | 문서 동기화 |

> 대조: todo 추적 제안서는 `state_effect`·리마인더 주입으로 **루프·엔진을 변경**했다. 메모리는 `system_prompt`/`tools` 생성자 인자와 도구 경로만 쓰므로 **코어 변경이 0**이다 — 이것이 D6(caller 소유) 선택의 직접적 이득이다.

---

## 10. 테스트 전략

- **단위**
  - `FileMemoryStore`: 섹션 파싱 왕복(save→read), `load_index()`가 본문 없이 메타데이터만, upsert(같은 name 재저장 시 갱신), `updated:` 유무에 따른 freshness.
  - `MemorySave/Read/Delete.call`: 스키마 검증, store 위임, read의 staleness caveat 부착(>1일).
  - `build_memory_section`: 빈 store → "empty" 문구, 항목 있으면 type별 인덱스 라인; 정적 지침이 동적 인덱스보다 앞.
  - `search`(기본): description/name substring 매칭.
- **통합 (fake provider)**
  - 모델이 `memory_save` 호출 → `FRIDAY_MEMORY.md`에 섹션 출현 → 다음 세션 `build_memory_section`에 인덱스 반영.
  - 모델이 `memory_read` 호출 → 본문 반환, 오래된 항목엔 caveat.
- **분산**
  - 세션 A에서 저장 → Store(파일/외부)에 영속 → "컨테이너 B"에서 `FridayAgent` 재구성 + `build_memory_section` 재조립 → 인덱스 신선 반영. LoopState 직렬화에 메모리 본문이 새지 않음(분리 확인).
- **무해성**
  - 메모리 부품 미배선 → friday 동작 기존과 동일(no-op).
