# Browser Control Bridge — Design Proposal

> **Status**: Proposal · not yet implemented
> **Created**: 2026-06-01
> **Scope**: Remote-control structure for server-side agent ↔ browser extension (client)
> **Related docs**: [02-tool-orchestration](../architecture/02-tool-orchestration.md) · [06-invariants](../architecture/06-invariants.md) · `friday_agent/tools/base.py`

---

## 1. Background & Problem

Friday is an SDK for **running the agent loop on the cloud/server side**. So the agent is not local and **cannot directly control** the user's browser. The browser is on the **client** side, the same side as an extension on the user's device.

We therefore need a structure in which the server agent communicates with the client to *direct* browser control and *collect the results*. This document proposes that **approach** — it records the design skeleton and decision rationale, not an implementation spec.

---

## 2. Key Insight — The Seam Already Exists

Half the answer is already in the current structure.

- `Tool.call(args) -> ToolResult` is just an async function (`tools/base.py`). The loop (`run_one_turn`) has no idea **where** a tool runs — `run_tools` simply awaits `call()`, takes the `ToolResult`, and carries it into the next turn.
- **The wire format already exists too.** Server→client is a serialized `tool_use` (name + input); client→server is a `tool_result` (data + is_error). That is, Friday's data model **becomes the server-client protocol as-is**.

> **Conclusion**: browser control means adding one kind of "remote tool". Instead of executing locally, `call()` just sends the command down to the extension and fetches the result back. The loop · orchestrator · Checkpoint are **unmodified**.

---

## 3. Design Principles

**Keep it in a host layer outside the SDK.** Just as the while-true driver was externalized to the caller (see [00-overview](../architecture/00-overview.md)), the browser bridge lives not in `friday_agent/core` but in a **host-side layer** (BYO tools + transport).

This is an SDK *application*, not an SDK *change*, and it is consistent with the Implementation Scope Charter — the core loop never knows whether a tool is remote.

---

## 4. Key Decisions

| Decision | Choice | Rationale | Rejected alternatives |
|---|---|---|---|
| **Persistence model** | A single always-on **stateful server + synchronous await** | Loop · Checkpoint unmodified. The remote tool's `call()` simply awaits the response future. Simplest. | **Tool-boundary suspend/resume** — more robust to process restarts · horizontal scaling, but suspending · resuming *mid*-turn requires machinery such as a new sentinel. Overkill at this stage. (→ §13 migration conditions) |
| **Control logic location** | **Hybrid** (primitive RPC + a few macros) | Fine-grained control via primitives; macros save turns on common flows. | **Pure thin** (multi-step blows up the turn count) · **pure thick** (logic scattered to the client, frequent extension updates, agent control ↓) |

---

## 5. Architecture Overview

### Placement

| Piece | Where it lives | What it is |
|---|---|---|
| Browser `Tool`s | Host (BYO tools) | The `tools/base.py` extension point as-is — `call()` goes out remotely |
| `RemoteBrowserBridge` + WS server | Host process | Session registry · pending futures · command routing |
| Extension | Client | WS client + Dispatcher (CDP mapping + macros) |

`friday_agent/` stays as-is.

### Data Flow (Single Command Round Trip)

```
LLM ──tool_use(browser_click,{selector})──► run_one_turn → run_tools → ClickTool.call(args)
                                                                            │
                                                              RemoteBrowserBridge
                                                                │ issue corr_id + register Future
                                                                │ registry[session_id] → conn
                                                                ▼
                         WS  {id, op:"click", params} ─────────────────────► Extension
                                                                              │ Dispatcher
                                                                              │ chrome.debugger(CDP)
                                                                              │  → Input.dispatchMouseEvent
                         WS  {id, ok:true, data} ◄──────────────────────────  execution result
                                                                │
                              Future.set_result(data) → ToolResult(data)
                                                                ▼
                              tool_result Message  (par-critical pair preserved) → next turn
```

Since this is a synchronous-await model, `call()` waits on the corr_id-keyed future with a timeout. No separate suspend sentinel or loop changes are needed.

---

## 6. Component Responsibilities

| Component | Responsibility |
|---|---|
| Browser `Tool`s | The tool surface the agent sees. `call()` builds a command envelope, delegates it to the bridge, and normalizes into a `ToolResult`. |
| `RemoteBrowserBridge` | Session→connection routing, corr_id↔Future management, timeout · failure→error result conversion. |
| WS server | Holds the persistent connection opened by the extension. Framing for commands down · results up. |
| Extension Dispatcher | Maps primitive ops to CDP/DOM calls, expands macro ops into compositions of primitives, replies with results. |

---

## 7. Tool Surface (Concretizing the Hybrid)

- **Primitives — split into individual tools** (alternative: a single `browser` dispatcher). E.g. `browser_navigate · click · type · read · screenshot · evaluate`. Splitting tools improves LLM affordance via typed schemas and lets the partitioner judge safety per tool.
  - **Concurrency**: on a single tab, nearly everything must be sequential (even reads risk racing with mutations). The conservative default `False` of `is_concurrency_safe` already behaves correctly, so leave it as-is ([02-tool-orchestration](../architecture/02-tool-orchestration.md)).
- **Macros — add a few tools**. E.g. `browser_fill_form · browser_extract · browser_wait_for`. One tool_use expands into multiple primitive ops inside the extension, saving turns on common flows.

---

## 8. Client Execution Mechanism

- **Recommended: map primitive ops to `chrome.debugger` (CDP)** — yields a computer-use-grade surface, from real input events · `Runtime.evaluate` · `Page.captureScreenshot` all the way to the accessibility tree. The cost is the "debugging" banner and the `debugger` permission.
- **Mixing is possible**: cheap DOM reads via content-script; input · eval · screenshots via CDP. Macros are extension JS routines that compose primitives.

---

## 9. Session Routing & Transport

- **The extension opens a persistent WS to the server first.** The extension is behind NAT and cannot accept inbound connections, so client-initiated is the only direction that works. The server sends commands down that socket, and result frames come back up the same socket (JSON-RPC-over-WS pattern).
- **Registry**: `session_id ↔ connection` binding. The agent looks up its connection by its own session_id.
- **Correlation**: each command carries a `corr_id`, and responses are matched via `corr_id ↔ Future`.
- **Handshake**: on connect, authenticate with the user token, then bind the session. (Details unresolved — §13)

---

## 10. Integrity & Failure Handling (par-critical)

Networks are unreliable, so disconnects · timeouts are treated as the **normal path**.

- **par-critical is preserved almost for free**: as long as `call()` returns *any* `ToolResult`, `run_tools` pairs up `tool_use`↔`tool_result`. Even if an exception leaks, the orchestrator catches it and `yield_missing_tool_result_blocks` (`core/loop.py`) backfills ([06-invariants](../architecture/06-invariants.md)). The bridge's only obligation is therefore to **never hang forever and convert every failure into an error `ToolResult`**.
- timeout → `ToolResult(is_error=True)`, disconnect → pending future reject → error result.
- **Idempotency pitfall**: even if the server gave up on a timeout, the client may have finished the command → retrying a mutating op risks **duplicate execution**. Mitigation: client-side corr_id dedup; mark mutating ops non-retryable.

---

## 11. Security Boundary

The extension executes server commands in the user's **logged-in** browser — a first-class trust surface.

- **Confirmation gate**: mutating actions (submit · payment · send) go through extension UI approval or an op/origin allowlist.
- **origin scoping**: restricts the sites the agent can drive.
- These are host policies, not a concern of the core loop.

---

## 12. Scope

| In scope | Out of scope (outside this proposal) |
|---|---|
| Remote tool seam · WS transport · session routing | Changes to `friday_agent/core` (all unmodified) |
| Hybrid tool surface (primitives + macros) | Tool-boundary suspend/resume (stateless resume) |
| par-critical failure mapping · security gate design | Concurrent multi-browser/multi-tab orchestration |

---

## 13. Open Questions

- **Authentication · pairing**: the concrete handshake for extension ↔ session binding (token issuance · verification · expiry).
- **Idempotency policy**: retry rules for mutating ops, client dedup retention period.
- **Macro catalog**: criteria for which flows to promote to macros, and the initial list.
- **Multi-tab/multi-browser**: routing · concurrency when one session handles multiple tabs.
- **suspend/resume migration conditions**: the triggers for moving to a tool-boundary suspend · resume model once the stateful server hits its limits (long-running actions · horizontal scaling · restart-durability requirements).
