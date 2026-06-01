# 브라우저 제어 브릿지 — 설계 제안서

> **상태**: 제안 (Proposal) · 구현 전
> **작성일**: 2026-06-01
> **범위**: 서버 사이드 에이전트 ↔ 브라우저 익스텐션(클라이언트) 원격 제어 구조
> **관련 문서**: [02-tool-orchestration](../architecture/02-tool-orchestration.md) · [06-invariants](../architecture/06-invariants.md) · `friday_agent/tools/base.py`

---

## 1. 배경 & 문제

Friday는 **클라우드/서버 사이드에서 에이전트 루프를 구동**하기 위한 SDK다. 그래서 에이전트는 로컬이 아니며, 사용자의 브라우저를 **직접 제어할 수 없다**. 브라우저는 사용자 기기의 익스텐션과 같은 **클라이언트** 쪽에 있다.

따라서 서버 에이전트가 클라이언트와 통신하며 브라우저 제어를 *지시*하고 *결과를 회수*하는 구조가 필요하다. 본 문서는 그 **접근 방식**을 제안한다 — 구현 명세가 아니라 설계 골격과 결정 근거를 기록한다.

---

## 2. 핵심 통찰 — 시임(seam)은 이미 존재한다

답의 절반은 현 구조에 이미 들어 있다.

- `Tool.call(args) -> ToolResult`는 그냥 async 함수다 (`tools/base.py`). 루프(`run_one_turn`)는 도구가 **어디서** 실행되는지 전혀 모른다 — `run_tools`가 `call()`을 await하고 `ToolResult`를 받아 다음 턴에 실어 보낼 뿐이다.
- **와이어 포맷도 이미 있다.** 서버→클라이언트는 직렬화된 `tool_use`(name + input), 클라이언트→서버는 `tool_result`(data + is_error)다. 즉 Friday의 데이터 모델이 **그대로 서버-클라이언트 프로토콜**이 된다.

> **결론**: 브라우저 제어는 "원격 도구" 한 종류를 추가하는 일이다. `call()`이 로컬 실행 대신 익스텐션으로 명령을 내려보내고 결과를 받아오면 된다. 루프·오케스트레이터·Checkpoint는 **무수정**.

---

## 3. 설계 원칙

**SDK 밖 호스트 레이어로 둔다.** while-true 드라이버를 호출자에게 외부화했듯([00-overview](../architecture/00-overview.md) 참조), 브라우저 브릿지도 `friday_agent/core`가 아니라 **호스트 측 레이어**(BYO 도구 + 트랜스포트)로 산다.

이것은 SDK *변경*이 아니라 SDK *응용*이며, 구현 스코프 헌장과 정합한다 — core 루프는 도구가 원격인지 끝까지 모른다.

---

## 4. 핵심 결정

| 결정 | 선택 | 근거 | 기각안 |
|---|---|---|---|
| **지속성 모델** | 단일 상시 **스테이트풀 서버 + 동기 await** | 루프·Checkpoint 무수정. 원격 도구 `call()`이 응답 future를 그냥 await. 가장 단순. | **도구 경계 suspend/resume** — 프로세스 재시작·수평 확장에 더 견고하나, 턴 *중간*에 중단·재개하려면 새 sentinel 등 기계장치가 필요. 현 단계엔 과함. (→ §13 이행 조건) |
| **제어 로직 위치** | **하이브리드** (원시 RPC + 매크로 소수) | 원시로 세밀 제어, 매크로로 흔한 흐름의 턴 수 절약. | **순수 thin**(멀티스텝이 턴 폭증) · **순수 thick**(로직이 클라이언트로 분산, 익스텐션 업데이트 잦고 에이전트 제어력 ↓) |

---

## 5. 아키텍처 개요

### 배치

| 조각 | 사는 곳 | 정체 |
|---|---|---|
| 브라우저 `Tool`들 | 호스트 (BYO 도구) | `tools/base.py` 확장점 그대로 — `call()`이 원격으로 나감 |
| `RemoteBrowserBridge` + WS 서버 | 호스트 프로세스 | 세션 레지스트리 · pending future · 명령 라우팅 |
| 익스텐션 | 클라이언트 | WS 클라이언트 + Dispatcher (CDP 매핑 + 매크로) |

`friday_agent/`는 그대로다.

### 데이터 흐름 (단일 명령 왕복)

```
LLM ──tool_use(browser_click,{selector})──► run_one_turn → run_tools → ClickTool.call(args)
                                                                            │
                                                              RemoteBrowserBridge
                                                                │ corr_id 발급 + Future 등록
                                                                │ registry[session_id] → conn
                                                                ▼
                         WS  {id, op:"click", params} ─────────────────────► Extension
                                                                              │ Dispatcher
                                                                              │ chrome.debugger(CDP)
                                                                              │  → Input.dispatchMouseEvent
                         WS  {id, ok:true, data} ◄──────────────────────────  실행 결과
                                                                │
                              Future.set_result(data) → ToolResult(data)
                                                                ▼
                              tool_result Message  (par-critical 쌍 유지) → 다음 턴
```

동기-await 모델이므로 `call()`은 corr_id 키 future를 timeout과 함께 기다린다. 별도 suspend sentinel·루프 수정이 필요 없다.

---

## 6. 컴포넌트 책임

| 컴포넌트 | 책임 |
|---|---|
| 브라우저 `Tool`들 | 에이전트가 보는 도구 표면. `call()`이 명령 봉투를 만들어 브릿지에 위임하고 `ToolResult`로 정규화. |
| `RemoteBrowserBridge` | 세션→연결 라우팅, corr_id↔Future 관리, timeout·실패→에러 결과 변환. |
| WS 서버 | 익스텐션이 건 영속 연결 보유. 명령 하향·결과 상향 프레이밍. |
| 익스텐션 Dispatcher | 원시 op를 CDP/DOM 호출로 매핑, 매크로 op를 원시 합성으로 전개, 결과 회신. |

---

## 7. 도구 표면 (하이브리드 구체화)

- **원시 — 개별 도구로 분리** (대안: 단일 `browser` 디스패처). 예: `browser_navigate · click · type · read · screenshot · evaluate`. 도구를 분리하면 타입드 스키마로 LLM 어포던스가 좋아지고, 파티셔너가 도구별로 안전성을 판정할 수 있다.
  - **동시성**: 단일 탭에선 거의 전부 순차여야 한다(읽기도 변경과 레이스 위험). `is_concurrency_safe`의 보수적 기본값 `False`가 이미 옳게 동작하므로 그대로 둔다 ([02-tool-orchestration](../architecture/02-tool-orchestration.md)).
- **매크로 — 소수 도구 추가**. 예: `browser_fill_form · browser_extract · browser_wait_for`. 한 tool_use가 익스텐션 안에서 다수 원시 op로 전개되어 흔한 흐름의 턴 수를 절약한다.

---

## 8. 클라이언트 실행 메커니즘

- **추천: 원시 op를 `chrome.debugger`(CDP)로 매핑** — 진짜 입력 이벤트·`Runtime.evaluate`·`Page.captureScreenshot`·accessibility 트리까지 computer-use급 표면을 얻는다. 대가는 "디버깅 중" 배너와 `debugger` 권한.
- **혼합 가능**: 싼 DOM 읽기는 content-script, 입력·eval·스크린샷은 CDP. 매크로는 원시를 합성하는 익스텐션 JS 루틴.

---

## 9. 세션 라우팅 & 트랜스포트

- **익스텐션이 서버로 영속 WS를 먼저 건다.** 익스텐션은 NAT 뒤라 inbound를 받지 못하므로, 클라이언트 발신이 유일하게 성립하는 방향이다. 서버는 그 소켓으로 명령을 내려보내고 결과 프레임이 같은 소켓으로 올라온다 (JSON-RPC-over-WS 패턴).
- **레지스트리**: `session_id ↔ connection` 바인딩. 에이전트는 자신의 session_id로 연결을 조회한다.
- **상관(correlation)**: 명령마다 `corr_id`를 실어 `corr_id ↔ Future`로 응답을 맞춘다.
- **핸드셰이크**: 연결 시 사용자 토큰으로 인증 후 세션 바인딩. (상세는 미해결 — §13)

---

## 10. 정합성 & 실패 처리 (par-critical)

네트워크는 불안정하므로 단절·타임아웃을 **정상 경로**로 취급한다.

- **par-critical은 거의 공짜로 보존된다**: `call()`이 *어떤* `ToolResult`든 반환하기만 하면 `run_tools`가 `tool_use`↔`tool_result` 쌍을 맞춘다. 예외가 새어도 오케스트레이터가 잡고 `yield_missing_tool_result_blocks`(`core/loop.py`)가 백필한다 ([06-invariants](../architecture/06-invariants.md)). 따라서 브릿지의 유일한 의무는 **영원히 멈추지 않고, 모든 실패를 에러 `ToolResult`로 변환**하는 것이다.
- timeout → `ToolResult(is_error=True)`, 단절 → pending future reject → 에러 결과.
- **멱등성 함정**: 서버가 timeout으로 포기했더라도 클라이언트는 명령을 끝냈을 수 있다 → 변경 op 재시도 시 **중복 실행** 위험. 완화: 클라이언트 corr_id 디둡, 변경 op는 non-retryable 표시.

---

## 11. 보안 경계

익스텐션은 사용자의 **로그인된** 브라우저에서 서버 명령을 실행한다 — 1급 신뢰 표면이다.

- **확인 게이트**: 변경성 액션(submit·결제·전송)은 익스텐션 UI 승인 또는 op/origin 화이트리스트를 거친다.
- **origin 스코핑**: 에이전트가 구동할 수 있는 사이트를 제한한다.
- 이는 호스트 정책이며 core 루프의 관심사가 아니다.

---

## 12. 스코프

| 포함 | 비범위 (이번 제안 밖) |
|---|---|
| 원격 도구 시임 · WS 트랜스포트 · 세션 라우팅 | `friday_agent/core` 수정 (전부 무수정) |
| 하이브리드 도구 표면 (원시 + 매크로) | 도구 경계 suspend/resume (스테이트리스 재개) |
| par-critical 실패 매핑 · 보안 게이트 설계 | 다중 브라우저/다중 탭 동시 오케스트레이션 |

---

## 13. 미해결 질문

- **인증·페어링**: 익스텐션 ↔ 세션 바인딩의 구체 핸드셰이크(토큰 발급·검증·만료).
- **멱등성 정책**: 변경 op의 재시도 규칙, 클라이언트 디둡 보존 기간.
- **매크로 카탈로그**: 어떤 흐름을 매크로로 승격할지 기준과 초기 목록.
- **다중 탭/다중 브라우저**: 한 세션이 여러 탭을 다룰 때의 라우팅·동시성.
- **suspend/resume 이행 조건**: 스테이트풀 서버가 한계에 닿는 시점(장시간 액션·수평 확장·재시작 내구성 요구)에 도구 경계 중단·재개 모델로 넘어가는 트리거.
