# AI-Sourcer 실전 적용 리포트 — vendored friday_agent 발산 분석

> **작성일**: 2026-07-22
> **비교 대상**: friday repo `friday_agent/` (HEAD `5d6931e`, 2026-06-09) ↔ ai-sourcer repo `src/adapters/friday_agent/` (HEAD `f7ed634`, 2026-07-21)
> **목적**: Friday SDK를 실제 프로덕트(ai-sourcer — 브라우저 제어 AI 소싱 에이전트)에 vendoring해 운영하면서 SDK 모듈 자체에 가한 변경을 전수 조사하고, 각 변경의 문제의식·실측 근거·업스트림(본 repo) 적용 가능성을 판정한다.

---

## 목차

1. [이 문서를 읽는 법](#1-이-문서를-읽는-법)
2. [배경: 이식·동기화 타임라인](#2-배경-이식동기화-타임라인)
3. [변경 총람 — 한눈에 보기](#3-변경-총람--한눈에-보기)
4. [사전 지식: 프롬프트 캐싱의 경제학](#4-사전-지식-프롬프트-캐싱의-경제학)
5. [변경 상세 분석](#5-변경-상세-분석)
   - 5.1 [동적 콘텐츠를 system에서 추방 — turn-local reminder 아키텍처](#51-동적-콘텐츠를-system에서-추방--turn-local-reminder-아키텍처)
   - 5.2 [캐시 breakpoint 리마인더-스킵](#52-캐시-breakpoint-리마인더-스킵)
   - 5.3 [엔진 확장 훅 2종 — system_sections · turn_sections](#53-엔진-확장-훅-2종--system_sections--turn_sections)
   - 5.4 [도구 스펙 와이어 다이어트](#54-도구-스펙-와이어-다이어트)
   - 5.5 [시스템 프롬프트 조립 순서 — 범용→구체](#55-시스템-프롬프트-조립-순서--범용구체)
   - 5.6 [메모리 서브시스템 opt-in 전환](#56-메모리-서브시스템-opt-in-전환)
   - 5.7 [(이식 비대상) GENERAL_AGENT_GUIDANCE 소서핏 축약](#57-이식-비대상-general_agent_guidance-소서핏-축약)
6. [변경되지 않은 것 — 아키텍처 검증](#6-변경되지-않은-것--아키텍처-검증)
7. [부수 자산: 테스트 스위트와 발산 기록 프랙티스](#7-부수-자산-테스트-스위트와-발산-기록-프랙티스)
8. [업스트림 적용 후보 판정](#8-업스트림-적용-후보-판정)
9. [맺음말: 세 가지 교훈](#9-맺음말-세-가지-교훈)

---

## 1. 이 문서를 읽는 법

이 리포트는 **Friday SDK 모듈 자체에 대한 변경만** 다룬다. ai-sourcer의 도메인 기능(LinkedIn 순회, 후보 평가, WebSocket 프로토콜, 클라이언트 배선 등)은 변경의 *맥락*으로만 등장하고 분석 대상이 아니다.

각 변경 절(§5.x)은 같은 구조를 따른다:

- **한 줄 요약** — 바쁘면 이것만 읽어도 된다.
- **문제** — 어떤 실전 증상에서 출발했는가 (가능한 한 실측 수치와 함께).
- **변경 내용** — 코드 수준에서 무엇이 어떻게 바뀌었는가.
- **업스트림과의 관계** — 본 repo에도 같은 문제가 존재하는가, 이식하면 무엇이 달라지는가.

§8의 판정표가 이 문서의 결론이다. 업스트림 적용을 검토할 때는 §8을 기준 삼아 항목별로 §5의 해당 절을 참조하면 된다.

**용어**:
- *vendored 사본* — ai-sourcer 저장소 안에 복사돼 들어간 `src/adapters/friday_agent/` 트리.
- *업스트림* — 본 repo의 `friday_agent/` 패키지.
- *turn-local reminder* — API 요청을 만들 때만 마지막 user 메시지에 덧붙는 `<system-reminder>` 텍스트 블록. `LoopState`에는 저장되지 않는다(비영속).

---

## 2. 배경: 이식·동기화 타임라인

vendored 사본은 6월 초 이식된 후 **6월 15일까지 업스트림과 완전 동기화**됐고, 7월 실전 운영 기간에 일방향으로 앞서나갔다. 즉 현재의 모든 차이는 "ai-sourcer 쪽에서 실전 문제를 풀며 추가된 것"이며, 업스트림에만 있고 vendored에 없는 변경은 존재하지 않는다.

| 날짜 | ai-sourcer 커밋 | 내용 |
|---|---|---|
| 06-02 | `8db435d` | friday_agent 최초 이식 (25파일, 2,441줄) |
| 06-05 | `1fbcdf4` | `docs/architecture` 9종을 모듈 내 `docs/`로 함께 vendoring |
| 06-09 | `31478fd` | **업스트림 동기화** — friday `1fd1f83`(always-on 캐싱 + thinking signature 라운드트립) 수용. 앱 쪽에 있던 캐싱 토글·mixin 클래스를 삭제하고 SDK 기능으로 대체 |
| 06-15 | `82e2ac1` | **업스트림 동기화** — friday `a1e3a04`(hooks 지침 제거 + 죽은 Terminal 사유 정리) 수용. **이 시점 = 마지막 완전 동기 상태** |
| 07-14 | `f8b0478` (#20) | 장부(ledger) 수집 아키텍처 구축. SDK에는 범용 `system_sections` 훅만 추가 (behavior-neutral) |
| 07-16 | `2a5265e` | **캐시 프리픽스 보존** — 메모리 인덱스·장부 인덱스를 turn-local reminder로 분리. `with_turn_reminders` 일반화, `turn_sections` 훅 신설 |
| 07-16 | `e3f840d` | 시스템 프롬프트 조립 순서 재배치(범용→구체) + 콜론 조항 보완 |
| 07-20 | `3bb07bb` | **캐시 breakpoint 리마인더-스킵** — tool_result 이중 캐시 쓰기 제거 (세션 캐시 쓰기 ~35% 낭비 실측) |
| 07-20 | `bf9721f` | 정적 프리픽스 다이어트 — `Tool.description` 분리, `_strip_titles`, 가이던스 축약 (17.5k → ~15.7k tok) |
| 07-20 | `966fb50` | 장부 아키텍처 **revert** — 단, SDK에 남긴 캐시 최적화·범용 훅은 전부 유지 |
| 07-21 | `f7ed634` | **메모리 opt-in 전환** + `_slim_wire_schema` 와이어 압축 (도구 스펙 콜당 −17%) |

타임라인에서 읽히는 서사 하나: **장부 아키텍처는 실험 끝에 되돌려졌지만, 그 실험이 SDK에 남긴 것(범용 훅, 캐시 구조 개선)은 revert 커밋에서 의도적으로 살아남았다.** 도메인 기능은 죽고 SDK 개선만 남은 — "무엇이 범용인가"의 판별이 실전에서 이미 한 번 이뤄진 셈이다.

---

## 3. 변경 총람 — 한눈에 보기

26개 파일 중 **소스 8개 + vendored docs 3개**가 다르다. 나머지는 바이트 동일(§6).

| 파일 | 변경 주제 | 관련 절 |
|---|---|---|
| `api/anthropic_provider.py` | 캐시 breakpoint 리마인더-스킵 | §5.2 |
| `api/prompts.py` | 조립 순서 뒤집기 + 가이던스 축약 | §5.5, §5.7 |
| `core/engine.py` | 시스템/턴 조립 재구성 · 훅 2종 · 메모리 opt-in | §5.1, §5.3, §5.6 |
| `core/loop.py` | `with_turn_reminders` 일반화 + `turn_reminders` 파라미터 | §5.1 |
| `memory/store.py` | `build_memory_section` → `MEMORY_INSTRUCTIONS` + `build_memory_reminder` 분리 | §5.1 |
| `memory/__init__.py` | 위 분리에 따른 export 변경 | §5.1 |
| `tools/base.py` | `Tool.description` · `_strip_titles` · `_slim_wire_schema` · `_dedent_text` | §5.4 |
| `tools/builtin/todo_write.py` | description 속성 이동 + 자명한 Field 설명 제거 | §5.4 |
| `docs/01-core-loop.md` `06-invariants.md` `08-memory.md` (vendored) | 위 변경 반영 + 발산 기록 테이블 | §7 |

변경의 동인을 세면 압도적으로 **비용(토큰) 최적화**다. ai-sourcer는 세션당 수십~수백 턴을 도는 장기 실행 에이전트라, 개발 중 LLM 호출 로그(`llm_calls_dev`)를 직접 분석하며 캐시 적중률과 프리픽스 크기를 튜닝했다. 그 과정에서 나온 개선이 이 문서의 본론이다.

---

## 4. 사전 지식: 프롬프트 캐싱의 경제학

§5.1~5.2를 이해하는 데 필요한 최소 배경이다. 이미 익숙하면 건너뛰어도 된다.

**Anthropic 프롬프트 캐싱의 규칙**:

1. API 요청은 `tools → system → messages` 순서로 직렬화된다. 이 전체가 하나의 "프리픽스 스트림"이다.
2. 요청 안에 `cache_control: {type: "ephemeral"}` 마커(**breakpoint**)를 최대 4개 둘 수 있다. 각 breakpoint는 "여기까지의 프리픽스를 캐시하라"는 뜻이다.
3. 캐시 **적중**하려면 그 breakpoint까지의 프리픽스가 이전 요청과 **바이트 단위로 동일**해야 한다. 중간의 한 글자만 바뀌어도 그 지점 이후의 모든 캐시가 무효화된다.
4. 비용: 캐시 **쓰기 = 입력 요금의 1.25×**, 캐시 **읽기 = 0.1×**, 캐시 안 탄 일반 입력 = 1×.

여기서 두 가지 실무 원칙이 나온다:

- **원칙 A — 프리픽스 앞쪽일수록 안정적이어야 한다.** system 프롬프트는 프리픽스 최상단에 있으므로, system이 한 바이트라도 바뀌면 *뒤따르는 대화 전체*의 캐시가 무효화된다. 긴 세션일수록 재작성 비용이 대화 길이에 비례해 커진다.
- **원칙 B — breakpoint는 "다음 요청에도 그대로 있을 위치"에 찍어야 한다.** 다음 턴에 사라질 블록에 breakpoint를 찍으면, 1.25×를 내고 쓴 캐시 엔트리를 한 번도 읽지 못한다.

업스트림 friday의 breakpoint 배치(`_apply_cache_control`)는 다음과 같다 — system 마지막 블록에 1개(도구+시스템 프리픽스), 마지막 두 메시지의 마지막 블록에 각 1개(대화 히스토리 롤링 앵커), 총 3개:

```
[tools] [system] [msg 0] [msg 1] ... [msg N-2] [msg N-1]
    └──────┬─────┘                       ▲          ▲
          BP1                          BP3         BP2
   (도구+시스템 캐시)              (안정 앵커)  (최신 히스토리)
```

ai-sourcer의 두 캐시 개선(§5.1, §5.2)은 각각 원칙 A와 원칙 B의 위반을 실측으로 찾아내 고친 것이다.

---

## 5. 변경 상세 분석

### 5.1 동적 콘텐츠를 system에서 추방 — turn-local reminder 아키텍처

> **한 줄 요약**: 매 턴 변하는 콘텐츠(메모리 인덱스)를 system 프롬프트에서 빼내 `<system-reminder>` 블록으로 messages[-1]에만 싣는다. system은 세션 내내 byte-stable해지고, 메모리 쓰기가 일어나도 대화 캐시가 살아남는다.
>
> **커밋**: `2a5265e` (07-16) · **파일**: `memory/store.py`, `memory/__init__.py`, `core/engine.py`, `core/loop.py`
>
> ✅ **업스트림 적용 완료** — 2026-07-22: `with_turn_reminders` 일반화는 `e3f4f8f`, 인덱스 분리+지침 압축(#8)은 `c07456b` (테스트·docs 01/08·CLAUDE.md 갱신 포함)

#### 문제

업스트림 friday에서 `engine.step()`은 매 턴 `build_memory_section(store)`를 호출해 **정적 지침(MEMORY_INSTRUCTIONS) + 라이브 메모리 인덱스**를 한 덩어리로 조립하고, 이를 시스템 프롬프트 뒤에 덧붙인다. 인덱스가 system 프리픽스 안에 있는 것이다.

읽기 전용 턴에서는 인덱스가 안 변하니 문제가 없다. 그러나 `memory_save`/`memory_delete`가 인덱스를 바꾸는 순간, **원칙 A 위반**이 발생한다:

```
system = [지침 + 인덱스]      ← save 발생 → 인덱스 한 줄 추가 → system 바이트 변경
                              → 프리픽스 최상단부터 불일치
                              → tools + system + "대화 전체"가 1.25×로 재작성
```

업스트림 문서(`docs/architecture/01-core-loop.md`)는 이를 "다음 턴 system tier가 1회 재기록된다"라고 적고 있는데, 이는 실제 비용을 과소평가한 표현이다. system은 프리픽스 *최상단*이므로 무효화는 system tier에 그치지 않고 **뒤따르는 대화 히스토리 캐시 전체에 전파**된다.

ai-sourcer에서는 이 문제가 장부(ledger) 인덱스로 증폭돼 드러났다: 개발 로그(`llm_calls_dev`) 분석에서 **LedgerSave 1회당 120k+ 토큰의 풀 캐시 재작성**이 실측됐다. 저장 도구를 자주 부르는 에이전트일수록, 세션이 길수록 비용이 폭증하는 구조다. 장부는 이후 revert됐지만 같은 구조 문제가 메모리 인덱스에 그대로 있었으므로 메모리 쪽 수정은 유지됐다.

#### 변경 내용

세 층을 관통하는 변경이다.

**(1) `memory/store.py` — 조립 함수 분리.** `build_memory_section()`(지침+인덱스 통합, system용)을 삭제하고 두 조각으로 나눴다:

- `MEMORY_INSTRUCTIONS` (모듈 상수) — 정적 지침만. 세션 내 불변이므로 system 프리픽스에 안전하게 들어간다. 내용도 함께 압축됐다(When to save / When to correct / When to access 3개 섹션 → "## Rules" 단일 목록). 빈 메모리일 때의 안내 문구("save memories as you learn...")도 인덱스 쪽에서 지침 쪽으로 이동했다 — 빈 상태 문구가 인덱스 자리에 있으면 "빈 상태 ↔ 첫 저장" 전환 시 그 자리 바이트가 바뀌기 때문.
- `build_memory_reminder(store)` (async) — 라이브 인덱스만 `<system-reminder>` 블록으로 렌더. 빈 store면 `""`을 반환해 블록 자체가 생기지 않는다.

```
<system-reminder>
Current memory index (rebuilt each turn from your saved memories):
- [user] user-role — ...
- [project] current-goal — ...
</system-reminder>
```

**(2) `core/loop.py` — 리마인더 주입 기계의 일반화.** 기존 `with_todo_reminder(messages, todos)`(todo 전용)를 범용 `with_turn_reminders(messages, texts)`로 확장했다:

```python
def with_turn_reminders(messages: list[Message], texts: list[str]) -> list[Message]:
    """여러 리마인더 텍스트 블록을 trailing user 메시지에 순서대로 병합.
    빈 문자열은 드롭. 원본 불변 — state.messages와 영속 LoopState는
    리마인더-프리 유지 (분산 재개 결정성)."""
```

`run_one_turn`에는 `turn_reminders: list[str] | None = None` 파라미터가 생겼다. API 요청 직전에 `[todo 리마인더] + turn_reminders`가 messages[-1]에 합쳐진다. 다음 `LoopState`는 깨끗한 원본 메시지에서 조립되므로 리마인더는 히스토리에 절대 새지 않는다(업스트림의 todo 리마인더 불변식이 그대로 일반화된 것).

docstring에 **캐시 불변식이 명문화**됐다: *"턴별 가변 텍스트는 전부 messages[-1]에만 싣는다 → messages[-2] 이전은 byte-stable → 프로바이더의 롤링 breakpoint가 계속 적중한다."*

**(3) `core/engine.py` — step()의 조립 재구성.** 정적/동적의 이분법이 코드 구조로 드러난다:

```python
# system 프리픽스: 정적 조각만 (세션 내 byte-stable — 캐시 프리픽스 생존 조건)
memory_section = MEMORY_INSTRUCTIONS if self._memory is not None else ""
parts = [p for p in (memory_section, self._system_prompt, *extra_sections) if p]
effective_prompt = "\n\n".join(parts)

# 동적 조각: 매 턴 재렌더 → turn-local reminder로 messages[-1]에
turn_reminders = [await build_memory_reminder(self._memory)] if self._memory else []
turn_reminders.extend([await build() for build in self._turn_sections])
```

#### 변경 전후 비교

| | 업스트림 (현재) | vendored (변경 후) |
|---|---|---|
| 지침(정적) | system 안 | system 안 (동일) |
| 인덱스(동적) | **system 안** | **messages[-1] 리마인더** |
| 메모리 쓰기 시 | system 변경 → **대화 전체 캐시 무효화** | system 불변 → 대화 캐시 유지, 변하는 건 리마인더 블록뿐 |
| LoopState serde | 불변 | 불변 (리마인더 비영속 동일) |
| compact 경로 | 인덱스 미주입 (요약에 안 샘) | 동일 |

#### 업스트림과의 관계

업스트림에 **같은 구조 문제가 그대로 존재**한다. 메모리가 always-on이므로 `memory_save`를 쓰는 모든 세션이 저장 시점마다 대화 전체 캐시를 재작성하고 있다. 이식하면 공개 API 면에서는 `build_memory_section` → `MEMORY_INSTRUCTIONS` + `build_memory_reminder` export 교체가 유일한 breaking change다. `docs/architecture/04-context-compaction.md` · `08-memory.md` · `01-core-loop.md`의 캐싱 서술 갱신이 동반돼야 한다.

---

### 5.2 캐시 breakpoint 리마인더-스킵

> **한 줄 요약**: breakpoint를 "메시지의 마지막 블록"이 아니라 "마지막 **영속** 블록"에 찍는다. 다음 턴에 사라질 리마인더 블록에 찍힌 breakpoint는 한 번도 적중하지 못하는 낭비 쓰기였다 — 세션 캐시 쓰기의 ~35% 실측.
>
> **커밋**: `3bb07bb` (07-20) · **파일**: `api/anthropic_provider.py`
>
> ✅ **업스트림 적용 완료** — 2026-07-22, friday `c985386` (테스트 6종 이식 + docs 03·06·CLAUDE.md 갱신 포함)

#### 문제

§4의 원칙 B 위반이다. 업스트림 `_apply_cache_control`은 messages[-1]과 [-2]의 **마지막 블록**에 무조건 breakpoint를 찍는다. 그런데 todo가 활성인 턴에는 messages[-1]의 마지막 블록이 turn-local todo 리마인더다. 이 블록은 다음 턴 API 뷰에서는 그 자리에 없다(리마인더는 매 턴 최신 내용으로 *새 위치*에 다시 붙는다):

```
턴 T   요청: ... [tool_result][todo 리마인더†]
                                    ▲ BP — 여기까지 1.25×로 캐시 쓰기

턴 T+1 요청: ... [tool_result][assistant 응답][새 tool_result][새 리마인더]
              → 리마인더†가 그 위치에서 사라짐
              → 턴 T에 쓴 캐시 엔트리와 프리픽스 불일치 → 그 엔트리는 영원히 미적중
              → 직전 턴의 tool_result가 턴 T+1에 "한 번 더" 1.25×로 캐시 쓰기됨
```

즉 리마인더를 마지막 블록으로 만든 순간부터, **모든 신규 tool_result가 두 번씩 캐시 쓰기**되고 있었다. ai-sourcer 실측(`data_9_1` 세션 분석): **세션 전체 캐시 쓰기의 약 35%가 이 낭비**였다.

이 문제는 §5.1이 리마인더 종류를 늘리면서(todo + 메모리 인덱스 + 장부) 표면화됐지만, **todo 리마인더 하나만 있어도 동일하게 발생**한다 — 업스트림도 todos 활성 세션에서 지금 이 비용을 내고 있다.

#### 변경 내용

`_apply_cache_control`이 블록을 고를 때 뒤에서부터 리마인더를 건너뛴다:

```python
_REMINDER_PREFIX = "<system-reminder>"          # loop.py 리마인더 규약 (기존)

@classmethod
def _is_turn_reminder(cls, block: dict) -> bool:
    return block.get("type") == "text" and \
           str(block.get("text", "")).startswith(cls._REMINDER_PREFIX)

# messages[-1]·[-2] 각각에 대해:
i = len(content) - 1
while i >= 0 and cls._is_turn_reminder(content[i]):
    i -= 1                                       # 리마인더(비영속) 스킵
if i >= 0:
    content[i] = {**content[i], "cache_control": mark}   # 마지막 '영속' 블록에 마크
# 전부 리마인더인 메시지는 마크 생략 — messages[-2] 앵커가 커버
```

결과: breakpoint는 항상 다음 턴에도 같은 자리에 있을 블록에 찍힌다(엔트리 재사용 가능). 리마인더 블록들은 breakpoint *뒤*의 일반 입력이 되어 매 턴 1×로 과금된다 — 낭비 캐시 쓰기(1.25×)보다 싸다. 호출당 비캐시 `input_tokens`가 3 → ~180+로 오르는 것은 이 트레이드오프의 의도된 결과다(리마인더 몸통이 일반 입력으로 이동). system·messages[-2] 앵커 동작은 불변.

#### 업스트림과의 관계

**업스트림에서 지금도 재현되는 낭비**이며, 수정은 Anthropic 어댑터 내부에 국한된다(공개 API 무변경, OpenAI 어댑터 무관). `<system-reminder>` 프리픽스 규약은 업스트림 `render_todo_reminder`가 이미 따르고 있으므로 판별 로직이 그대로 동작한다. vendored 쪽에 회귀 테스트 6종이 함께 있다(§7). 판정표에서 **최우선 이식 후보**다.

---

### 5.3 엔진 확장 훅 2종 — system_sections · turn_sections

> **한 줄 요약**: 호출자가 SDK를 포크하지 않고 시스템 프롬프트(정적)와 턴 리마인더(동적)에 도메인 콘텐츠를 주입할 수 있는 공식 확장점. §5.1의 "정적은 system, 동적은 리마인더" 이분법을 API 면으로 노출한 것.
>
> **커밋**: `f8b0478` + `2a5265e` · **파일**: `core/engine.py`

#### 문제

ai-sourcer는 장부 수집 규칙(정적)과 장부 인덱스+숏리스트(동적)를 에이전트 컨텍스트에 넣어야 했다. SDK에 주입 경로가 없으니 선택지는 둘: `engine.step()` 주변에서 messages를 직접 조작하거나(캐시 불변식을 호출자가 재발명해야 함), SDK를 수정하거나. 후자를 **범용 훅 형태**로 수행한 것이 이 변경이다.

#### 변경 내용

```python
FridayAgent(
    ...,
    system_sections: list[Callable[[], Awaitable[str]]] | None = None,
    turn_sections:   list[Callable[[], Awaitable[str]]] | None = None,
)
```

- **`system_sections`** — 매 턴 async로 렌더돼 (메모리 지침 →) 도메인 프롬프트 뒤에 순서대로 합쳐진다. 빈 문자열은 필터. docstring에 **byte-stable 계약이 명문화**돼 있다: *"세션 내 출력이 바이트 단위로 안정적이어야 한다. system은 캐시 프리픽스 최상단이라 변경 시 대화 캐시 전체가 무효화된다. 턴마다 변하는 콘텐츠는 turn_sections로."*
- **`turn_sections`** — 매 턴 렌더돼 turn-local reminder로 messages[-1]에 실린다(todo → 메모리 인덱스 → turn_sections 순). `LoopState`에 비영속.
- 기본값(양쪽 None/빈 리스트)이면 출력이 훅 이전과 **바이트 동일**(behavior-neutral) — 순수 additive 변경이다.

#### 업스트림과의 관계

이 훅이 실전에서 가치를 증명한 방식이 흥미롭다: 장부 아키텍처는 결국 revert됐지만(`966fb50`), **훅 자체는 범용이라 살아남았다**. 즉 "도메인 실험을 SDK 포크 없이 가능하게 하는 층"으로서의 효용이 실증된 셈이다.

업스트림 관점의 유일한 쟁점은 스코프 헌장이다. `docs/architecture/00-overview.md`의 제외 항목("임의로 다시 추가하지 말 것")에 저촉되는 신규 기능은 아니지만, 공개 API 면이 넓어지는 것은 사실이다. 실사용 검증이 있으므로 "시스템 프롬프트 조립 machinery"(포함 항목)의 자연스러운 확장으로 볼 근거는 충분하다 — §8에서 "이식 권장, 논의 1건"으로 판정.

---

### 5.4 도구 스펙 와이어 다이어트

> **한 줄 요약**: 도구 스키마에서 모델에게 정보가 0인 바이트(pydantic 자동 title, Optional 3중 세리머니, 소스 들여쓰기, 개발자용 docstring)를 걷어낸다. 콜당 도구 스펙 2,471 → 2,041 tok (−17%), 의미·검증 완전 불변.
>
> **커밋**: `bf9721f` + `f7ed634` · **파일**: `tools/base.py`, `tools/builtin/todo_write.py`
>
> ✅ **업스트림 적용 완료** — 2026-07-22, friday `bfbe7f4` (테스트 10종 이식 + docs 02 갱신 포함)

#### 문제

도구 스키마는 **모든 API 호출의 프리픽스 최상단**에 실린다. 캐시 읽기(0.1×)로 할인되긴 하지만 첫 호출과 캐시 미스마다 전액이고, 무엇보다 컨텍스트 윈도우를 상시 점유한다. ai-sourcer가 도구 22종의 와이어 페이로드를 뜯어보니 모델에게 무의미한 바이트가 네 종류 발견됐다:

1. **pydantic 자동 title** — 모델/필드마다 `"title": "TodoWriteInput"`, `"title": "Todos"` 같은 cosmetic title이 자동 생성된다. 프로퍼티 이름이 이미 스키마에 있으므로 순수 중복. (업스트림은 최상위 title 1개만 `pop`하고 중첩 title은 그대로 내보낸다.)
2. **Optional 3중 세리머니** — `Optional[int] = None` 필드 하나가 `"anyOf": [{"type": "integer"}, {"type": "null"}], "default": null` 로 직렬화된다. "필수 아님"은 `required` 배열 부재가 이미 전달하는 정보다.
3. **docstring 유출** — 도구 description이 클래스 docstring에서 오는데, docstring에는 개발자용 구현 노트(RPC 배선, 서버 내부 동작)가 섞이기 마련이고 그게 전부 매 요청 와이어로 나간다.
4. **들여쓰기 유출** — triple-quote 설명문의 소스 들여쓰기(`"...\n    continued"`)가 와이어에 그대로 실린다.

#### 변경 내용

`tools/base.py`에 독립적인 장치 4개가 추가되고 `get_tool_schema()`가 이를 모두 적용한다:

```python
class Tool(ABC):
    name: str
    description: str = ""     # ← 신설: 모델 대면 설명. 미설정 시 docstring 폴백(하위호환)

    def get_tool_schema(self) -> dict:
        schema = _inline_defs(self.input_schema().model_json_schema())
        _strip_titles(schema)        # cosmetic title 재귀 제거
        _slim_wire_schema(schema)    # Optional 접기 + null default 제거 + desc dedent
        return {
            "name": self.name,
            "description": _dedent_text(self.description or (self.__class__.__doc__ or "")),
            "input_schema": schema,
        }
```

- **`_strip_titles`** — 문자열 값인 `title` 키만 재귀 제거. `title`이라는 이름의 *실제 프로퍼티*는 dict 값이므로 살아남는다(경계 케이스 테스트 존재).
- **`_slim_wire_schema`** — `anyOf: [X, {type: null}]` 2원소 union을 non-null 멤버로 접고(형제 키 충돌 시 description 등 기존 키 우선), `default: null` 제거, description dedent. **비-null union과 의미 있는 default는 보존.** 검증은 여전히 Pydantic 모델이 수행하므로 의미 불변 — 이 스키마는 순수 "모델에게 보여주는 문서"다.

  ```json
  // before                                          // after
  {"anyOf": [{"type": "integer"},                    {"type": "integer",
              {"type": "null"}],                      "description": "max items"}
   "default": null,
   "title": "Count",
   "description": "max items"}
  ```

`tools/builtin/todo_write.py`는 이 장치의 첫 적용 사례다: 모델 대면 설명을 `description` 속성으로 옮기고(docstring은 개발자 노트로 전환), enum 값을 되풀이하던 자명한 Field 설명을 제거했다. 단 "ENTIRE list every call (full replace)" 같은 **load-bearing 규칙은 description에 유지** — 다이어트의 선이 "정보량 0인 바이트"에 그어져 있다.

**실측**: ai-sourcer 도구 카탈로그 기준 콜당 2,471 → 2,041 tok (−17%). (이 수치에는 앱 특화 도구 2종 미장착분이 섞여 있으므로 SDK 장치 단독 효과는 그보다 작지만, 도구 수가 많은 카탈로그일수록 커지는 구조다.)

#### 업스트림과의 관계

완전히 LLM-agnostic·도메인-무관한 개선이고 하위호환이다(description 미설정 시 기존 docstring 폴백). vendored 쪽 스키마 테스트(`test_tool_schema.py`, description 우선순위·title 경계·와이어 압축 6종)를 함께 가져오면 된다. **그대로 이식 판정.**

---

### 5.5 시스템 프롬프트 조립 순서 — 범용→구체

> **한 줄 요약**: SDK 범용 가이던스를 앞에, 호출자 도메인 프롬프트를 뒤에 둔다(recency 우위는 구체 규칙에게). 도메인 발화 정책이 SDK 가이던스에 밀려 절반만 먹히던 실전 문제의 해결.
>
> **커밋**: `e3f840d` · **파일**: `api/prompts.py`, `core/engine.py`
>
> ✅ **업스트림 적용 완료** — 2026-07-22, friday `52942d5` (#6 조립 순서 + #7 콜론 조항, 순서 테스트 이식 + docs 01/03/08 갱신 포함)

#### 문제

업스트림 조립 순서는 `호출자 도메인 프롬프트 → GENERAL_AGENT_GUIDANCE → TODO_GUIDANCE`다. LLM은 프롬프트에서 **나중에 온 지시에 recency 가중**을 주는 경향이 있는데, 이 순서에서는 SDK의 범용 지시가 마지막 발언권을 갖는다. ai-sourcer 실전에서 도메인 프롬프트의 발화 빈도 정책이 SDK 범용 톤 가이드에 밀려 절반만 지켜지는 증상(`data_5` 세션)으로 확인됐다.

#### 변경 내용

두 층 모두 **범용 → 구체** 순으로 뒤집었다:

```python
# api/prompts.py — assemble_system_prompt
- blocks = [b for b in (system_prompt, GENERAL_AGENT_GUIDANCE, TODO_GUIDANCE) if b]
+ blocks = [b for b in (GENERAL_AGENT_GUIDANCE, TODO_GUIDANCE, system_prompt) if b]
```

엔진 층까지 합친 최종 순서: `GENERAL → TODO → MEMORY_INSTRUCTIONS → 도메인 프롬프트 → system_sections`. 일반 원칙이 바닥을 깔고, 도메인 규칙이 마지막에 와서 충돌 시 이긴다.

함께 들어간 소형 개선(범용): GENERAL의 콜론 조항 보완. 기존 "Let me check the file." 예시가 *매 도구 호출마다 한 줄씩 발화하는 표준형*처럼 학습되는 부작용이 있어, "다음 행동이 문맥상 자명하면 텍스트 없이 도구만 호출하라(announcing each step is noise)"를 추가했다.

#### 업스트림과의 관계

원칙 자체(범용→구체, 구체가 recency 우위)는 도메인 무관하게 타당하고, breaking change도 아니다(조립 결과 문자열만 바뀜). 다만 **동작(모델 응답 성향)이 변하는 변경**이므로 이식 시 아키텍처 문서의 조립 순서 서술 갱신과 함께 의식적으로 결정할 것. 콜론 조항 보완은 독립적으로 이식 가능한 소형 개선이다.

---

### 5.6 메모리 서브시스템 opt-in 전환

> **한 줄 요약**: `FridayAgent(memory=None)`(기본)이면 메모리 도구 3종·지침·인덱스 리마인더가 전부 미장착된다. 업스트림의 "always-on 빌트인" 설계를 뒤집는 유일한 변경 — 근거는 "전 세션 실측 호출 0회, 매 콜 ~1k tok 사장".
>
> **커밋**: `f7ed634` · **파일**: `core/engine.py` (+ vendored docs 발산 기록)

#### 문제

업스트림 설계에서 메모리는 **always-on 빌트인**이다: `memory=None`이면 기본 `FileMemoryStore`가 자동 장착되고, 도구 3종(`memory_save`/`memory_read`/`memory_delete`)과 MEMORY_INSTRUCTIONS·인덱스가 항상 주입된다(opt-out 없음).

ai-sourcer 운영 실측: **전 세션에서 메모리 도구 호출 0회.** 그런데 지침+인덱스+도구 스키마로 **매 콜 ~1k 토큰**이 상시 지출됐다. 소싱 에이전트의 세션은 단발성 작업(후보 N명 수집·평가)이라 세션 경계를 넘겨 기억할 것이 구조적으로 없었다 — "장기 기억"이라는 기능 자체가 이 도메인에서 무용했던 것.

#### 변경 내용

```python
# core/engine.py — __init__
- self._memory = memory if memory is not None else FileMemoryStore()   # always-on
+ self._memory = memory                                                # opt-in

- assembled = [*caller_tools, *builtin_tools(), *self._memory.tools()]
+ memory_tools = self._memory.tools() if self._memory is not None else []
+ assembled = [*caller_tools, *builtin_tools(), *memory_tools]
```

`step()`도 대칭적으로: store가 없으면 MEMORY_INSTRUCTIONS도, 인덱스 리마인더도 만들지 않는다. 장착하려면 `FridayAgent(memory=FileMemoryStore())`를 명시한다. 코어 루프·`LoopState` serde·`memory/` 모듈 코드는 전부 불변 — 바뀐 것은 "기본 장착 여부"라는 정책뿐이다.

#### 업스트림과의 관계

이 변경만 성격이 다르다. §5.1~5.5가 "같은 설계를 더 싸고 단단하게"라면, 이것은 **명시적 설계 결정의 반전**이다(CLAUDE.md·`docs/architecture/08-memory.md`가 "always-on 빌트인, opt-out 없음"을 명문화하고 있다). 판단 재료:

- **찬성 쪽**: 실측이 명확하다(0회 호출에 매 콜 ~1k tok). "모든 도메인에 항상 유용한 기능"이라는 전제가 최소 한 개 실제 도메인에서 반증됐다. 도메인-무관 SDK가 특정 기능을 강제 장착하는 것 자체가 스코프 철학과 긴장 관계라는 시각도 가능하다.
- **신중 쪽**: ai-sourcer는 단발성 세션 도메인이라 메모리 무용이 *도메인 특성*일 수 있다. 장기 대화형 도메인에서는 always-on이 "기본으로 켜져 있어야 쓰이기 시작하는" 기능일 수 있다. 전환 시 기본 동작이 바뀌는 breaking change이며 문서·헌장 수정 동반.

vendored docs는 이 발산을 `06-invariants.md`의 "Vendored 발산 기록" 테이블에 날짜·사유·"업스트림 갱신 시 이 semantics를 보존할 것" 지침과 함께 기록해 뒀다(§7). §8에서 **"논의 필요"** 판정.

---

### 5.7 (이식 비대상) GENERAL_AGENT_GUIDANCE 소서핏 축약

> **한 줄 요약**: SDK 범용 가이던스에서 안전 문단("Executing actions with care", obstacle 대응)과 URL 추측 금지 라인을 삭제하고 톤 문단을 압축했다. **ai-sourcer의 "도구 전부 읽기 전용" 전제에 기댄 축약이라 범용 SDK에는 부적합** — 기록 목적으로만 정리한다.
>
> **커밋**: `bf9721f` (프리픽스 다이어트 L4) · **파일**: `api/prompts.py`

vendored 사본의 GENERAL_AGENT_GUIDANCE는 업스트림 대비 크게 짧다. 삭제 사유가 소스 주석에 남아 있는데, 핵심은 전부 *도메인 전제*다:

- 안전 문단 삭제 — "현재 도구는 읽기·탐색 전용이라 미적용". 주석에 ⚠️ 경고가 박혀 있다: *"발송류 도구(1촌 신청·InMail·메시지 전송)가 추가되면 confirmation-first 문단을 반드시 복원할 것 — 승인 플로우 미구현 상태에서 유일한 범용 브레이크였다."*
- URL 추측 금지 라인 삭제 — 도메인 규칙(검색 URL 조립 금지, href 기반 이동)이 더 구체적으로 소유.
- 장문 톤/효율 문단 → "# Output style" 압축 — 도메인 프롬프트의 톤 정책이 소유.

이 항목이 주는 교훈은 콘텐츠가 아니라 방법론이다: **SDK 기본 가이던스와 도메인 프롬프트 사이의 "소유권 분배"는 도메인마다 다르게 그어진다.** 범용 SDK는 안전 문단을 기본 포함하되(도구가 무엇일지 모르므로), 프리픽스에 민감한 호출자가 축약할 길은 — 현재도 가이던스가 상수라 몽키패치 외엔 없다. 이를 공식화할지(예: 가이던스 오버라이드 인자)는 별도 논의 거리로 남긴다(§8 후보 목록 외).

---

## 6. 변경되지 않은 것 — 아키텍처 검증

전체 26개 소스 파일 중 17개가 **바이트 동일**이다. 무엇이 안 바뀌었는지가 무엇이 바뀌었는지만큼 중요하다 — 실프로덕트의 압력을 통과하고도 수정이 필요 없었던 부분이기 때문이다.

| 무변경 영역 | 파일 | 시사점 |
|---|---|---|
| 코어 턴 루프 상태·serde | `core/state.py` | `LoopState` 직렬화·분산 재개 설계가 실전에서 그대로 유효 |
| 도구 오케스트레이션 | `tools/orchestrator.py` | 파티셔닝·병렬 실행·순서 보존이 브라우저 제어 도구군에서도 무수정 동작 |
| 컨텍스트 컴팩션 | `context/compact.py` | 호출자 주도 compact 경계 유효 |
| 메시지 모델·정규화 | `messages/types.py`, `normalize.py` | 단일 flat Message/ContentBlock 모델로 충분했음 |
| 프로바이더 경계 | `api/provider.py`, `configs.py` | LLMProvider ABC 면 무수정 |
| **OpenAI 어댑터** | `api/openai_provider.py` | 캐시 최적화 전 과정에서 무변경 — "캐시 배치는 Anthropic 어댑터 로컬 관심사"라는 경계 설계가 정확히 맞았음 (OpenAI는 자동 캐싱) |
| 메모리 도구·백엔드 | `memory/tool.py` | 도구 3종 구현 무수정 — 바뀐 건 장착 정책(§5.6)과 인덱스 배치(§5.1)뿐 |

한 줄로: **수정은 전부 "프롬프트를 어떻게 배치·과금하느냐" 층에서 일어났고, "루프를 어떻게 돌리느냐" 층은 건드릴 필요가 없었다.**

---

## 7. 부수 자산: 테스트 스위트와 발산 기록 프랙티스

**전용 회귀 테스트 (업스트림에 없는 자산).** vendored 변경은 `tests/unit/adapters/friday_agent/` 아래 7파일 826줄의 테스트로 고정돼 있다. 이식 시 해당 항목과 함께 가져올 수 있다:

| 테스트 파일 | 고정하는 동작 | 관련 절 |
|---|---|---|
| `test_anthropic_cache_control.py` (89줄) | breakpoint 리마인더-스킵 6종 (다중 리마인더·전부-리마인더 방어·기존 동작 회귀) | §5.2 |
| `test_loop_turn_reminders.py` (160줄) | `with_turn_reminders` 병합·순서·비영속 | §5.1 |
| `test_memory_reminder.py` (63줄) | `build_memory_reminder` 렌더·빈 store → `""` | §5.1 |
| `test_engine_system_sections.py` (184줄) | 섹션 순서 보존·빈 문자열 필터·기본값 출력-중립 | §5.3 |
| `test_engine_turn_sections.py` (130줄) | turn_sections 주입 경로 | §5.3 |
| `test_tool_schema.py` (165줄) | description 우선순위·title 경계 케이스·와이어 압축 6종 | §5.4 |
| `test_assemble_system_prompt.py` (35줄) | 조립 순서 | §5.5 |

**Vendored 발산 기록 프랙티스.** vendored docs의 `06-invariants.md`에 "Vendored 발산 기록" 테이블이 신설돼, 업스트림과 의도적으로 다른 지점(현재 메모리 opt-in 1건)을 날짜·변경·사유·"업스트림 갱신 시 보존할 것" 지침으로 관리한다. SDK를 vendoring하는 소비자 쪽 관리 기법으로, 업스트림 코드 대상은 아니지만 README의 vendoring 가이드에 소개할 만한 패턴이다.

---

## 8. 업스트림 적용 후보 판정

각 항목의 상세 논거는 해당 절 참조. 판정 기준: **범용성**(도메인 전제 없이 성립하는가) · **근거**(실측이 있는가) · **위험**(breaking change·설계 결정 반전 여부).

| # | 항목 | 절 | 판정 | 요지 |
|---|---|---|---|---|
| 1 | 캐시 breakpoint 리마인더-스킵 | §5.2 | ✅ **적용 완료** (2026-07-22, `c985386`) | 업스트림에서 todo 리마인더로 지금도 재현되는 낭비(~35% 실측). Anthropic 어댑터 국한, API 무변경, 테스트 동반 |
| 2 | 메모리 인덱스 turn-local 분리 | §5.1 | ✅ **적용 완료** (2026-07-22, `c07456b`) | 업스트림의 "system tier 1회 재기록"은 실제론 대화 전체 무효화(120k+ 실측). export 1건 교체 + docs 갱신 동반 |
| 3 | `with_turn_reminders` + `turn_reminders` 파라미터 | §5.1 | ✅ **적용 완료** (2026-07-22, `e3f4f8f`) | #2의 전제 기계. todo 전용 로직의 자연스러운 일반화, 캐시 불변식 명문화 포함 |
| 4 | `system_sections` · `turn_sections` 훅 | §5.3 | ✅ **이식 권장 (논의 1건)** | 범용 확장점으로 실전 검증(장부 실험이 SDK 포크 없이 수행·revert됨). 쟁점은 API 면 확장 vs 스코프 헌장 — "프롬프트 조립 machinery"의 확장으로 볼 근거 충분 |
| 5 | 도구 스펙 다이어트 4종 + TodoWrite 적용 | §5.4 | ✅ **적용 완료** (2026-07-22, `bfbe7f4`) | LLM-agnostic·의미 불변·하위호환·−17% 실측·테스트 동반 |
| 6 | 조립 순서 범용→구체 | §5.5 | ✅ **적용 완료** (2026-07-22, `52942d5`) | 원칙 범용 + 실전 근거. 단 모델 응답 성향이 변하므로 의식적 결정 + docs 갱신 |
| 7 | 콜론 조항 보완 ("자명하면 무발화") | §5.5 | ✅ **적용 완료** (2026-07-22, `52942d5`) | 소형·독립·범용 |
| 8 | MEMORY_INSTRUCTIONS 압축 | §5.1 | ✅ **적용 완료** (2026-07-22, `c07456b`, #2와 함께) | 내용이 turn-local reminder 구조를 전제 — 단독 이식 불가 |
| 9 | 메모리 opt-in 전환 | §5.6 | ⚠️ **논의 필요** | 실측 근거 강력(호출 0회·~1k tok/콜)하나 명시적 설계 결정의 반전 + breaking change. 도메인 특성 가능성 검토 필요 |
| 10 | GENERAL_AGENT_GUIDANCE 축약 | §5.7 | ❌ **제외 (소서핏)** | 읽기 전용 도구 전제의 안전 문단 삭제 — 범용 SDK 부적합. "가이던스 오버라이드를 공식화할 것인가"는 별도 논의 거리 |
| 11 | Vendored 발산 기록 프랙티스 | §7 | 📖 **참고** | 코드 아닌 vendoring 관리 기법. README vendoring 가이드 소재 |

**권장 이식 순서**: #1·#3·#2·#8(캐시 계열, 상호 의존) → #5(독립) → #6·#7(프롬프트 조립) → #4(API 면 확장 논의 후) → #9(설계 논의).

---

## 9. 맺음말: 세 가지 교훈

**1. 실전 개선의 무게중심은 "프롬프트 캐시 경제학"이었다.** 7주 운영(6/2 이식 ~ 7/21)에서 나온 SDK 수정의 대부분이 두 질문으로 수렴한다 — *무엇이 byte-stable해야 하는가*(system 프리픽스), *동적 콘텐츠는 어디에 실어야 하는가*(messages[-1] turn-local reminder). 장기 실행 에이전트 SDK에서 캐시 배치는 부가 기능이 아니라 비용 구조의 본체다. 낭비는 로그 실측(`llm_calls_dev` 분석)으로만 보였다 — 정상 동작하는 시스템이라 기능 테스트로는 영원히 안 잡혔을 것이다.

**2. 코어 아키텍처는 무수정 생존했다.** 턴 루프·LoopState serde·오케스트레이터·compact 경계·프로바이더 ABC — 실프로덕트의 압력에서 단 한 줄도 바뀌지 않았다. 특히 캐시 최적화 전 과정에서 OpenAI 어댑터가 무변경이었다는 사실은 "캐시 배치는 벤더 어댑터 로컬 관심사"라는 경계 설계의 정확성을 실증한다.

**3. "범용인가"의 판별은 실전이 해줬다.** 장부 아키텍처(도메인 기능)는 revert로 죽고, 그 실험을 지탱한 훅(범용 기계)은 살아남았다. 메모리 always-on(범용이라 가정했던 기능)은 실측 0회 호출로 반증 후보가 됐다. 이 리포트의 판정표는 그 실전 판별의 기록이며, 업스트림 적용은 이 문서를 기준으로 항목별로 진행한다.
