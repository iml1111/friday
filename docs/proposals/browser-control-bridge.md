# Browser Control Bridge — Design Proposal

> **Status**: Proposal · not yet implemented
> **Created**: 2026-06-01
> **Scope**: Remote-control structure for server-side agent ↔ browser extension (client)
> **Related docs**: [02-tool-orchestration](../architecture/02-tool-orchestration.md) · [04-context-compaction](../architecture/04-context-compaction.md) · [06-invariants](../architecture/06-invariants.md) · `friday_agent/tools/base.py`

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
| **Persistence model** | A single always-on **stateful server + synchronous await** | Loop · Checkpoint unmodified. The remote tool's `call()` simply awaits the response future. Simplest. | **Tool-boundary suspend/resume** — more robust to process restarts · horizontal scaling, but suspending · resuming *mid*-turn requires machinery such as a new sentinel. Overkill at this stage. (→ §14 migration conditions) |
| **Control logic location** | **Hybrid** (primitive RPC + a few macros) | Fine-grained control via primitives; macros save turns on common flows. | **Pure thin** (multi-step blows up the turn count) · **pure thick** (logic scattered to the client, frequent extension updates, agent control ↓) |
| **Command granularity** | **Stepwise primitives (A) as the foundation + linear batch (C) as an option** | A interacts robustly with pages that change in real time; C saves turns on deterministic sequences. C is just A sent ahead of time — same executor. | **pure-A** (wastes turns on long deterministic flows) · **pure-C** (blind during the batch → fragile on dynamic pages) · **branching/DSL batch** (needs a client interpreter; future) |

---

## 5. Architecture Overview

### Placement

| Piece | Where it lives | What it is |
|---|---|---|
| Browser `Tool`s | Host (BYO tools) | The `tools/base.py` extension point as-is — `call()` goes out remotely |
| `RemoteBrowserBridge` + WS server | Host process | Session registry · pending futures · command routing |
| Extension | Client | WS client + Dispatcher (CDP mapping + batch execution + macros) |

`friday_agent/` stays as-is.

### Data Flow (Single Command Round Trip, Granularity A)

```
LLM ──tool_use(browser_click,{ref})──► run_one_turn → run_tools → ClickTool.call(args)
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

**Batch variant (granularity C)**: `browser_run_steps([navigate, wait_for, type, click])` — 1 tool_use = 1 WS frame = a list of N primitives. The extension executes them sequentially and replies with **1 aggregated result** (stop rules in §8.4). The flow is the same as above; the only differences are that the op is a list and the result is an aggregate. par-critical stays at 1 pair (N sub-results).

Since this is a synchronous-await model, `call()` waits on the corr_id-keyed future with a timeout. No separate suspend sentinel or loop changes are needed.

---

## 6. Component Responsibilities

| Component | Responsibility |
|---|---|
| Browser `Tool`s | The tool surface the agent sees (§7). `call()` builds a command envelope, delegates it to the bridge, and normalizes into a `ToolResult`. |
| `RemoteBrowserBridge` | Session→connection routing, corr_id↔Future management, timeout · transport failure→error result conversion. |
| WS server | Holds the persistent connection opened by the extension. Framing for commands down · results up. |
| Extension Dispatcher | Maps primitive ops to CDP/DOM calls · batch (C) sequential execution/aggregation/stopping · ref↔current DOM validation · observation snapshot generation · macro expansion · result reply. |

---

## 7. Tool Surface

Tools fall into four groups, **orthogonal to command granularity (A/C)**.

### 7.1 Granularity — A / C / Macro

| | Transmission unit | Where sequence intelligence lives | LLM involvement |
|---|---|---|---|
| **A. Stepwise primitive** | tool_use 1 = WS 1 = primitive 1 | **LLM** (every turn) | Every step |
| **C. Server-assembled batch** | tool_use 1 = WS 1 = **list of N** primitives | **Agent** (assembled on the fly → sent as data) | Once, at batch start |
| B. Macro | tool_use 1 = WS 1 = name 1 | **Client** (built in) | One call by name |

> **C is just A sent ahead of time.** The client executor is the same; all C newly needs is (1) a tool that accepts a list and (2) sequential execution · aggregation. That is, A is the foundation and C is a thin batch tool layered on top. Macros (B) are a separate thing whose sequence is *built into* the client — "batch = macro" does not hold.

### 7.2 Observation Tools (Foundation of A)

To interact robustly with pages that change in real time, the agent **must be able to read the current state**. This is the foundation of the `act → read → adapt` loop.

- `browser_read` — sends the current page structure to the server (representation format in §8.1).
- `browser_screenshot` — complements vision models (coordinate-based).

### 7.3 Primitive Action Tools

- `browser_navigate · click · type · evaluate …`
- **ref-based targeting recommended**: take element refs assigned by observation (§8.1) to avoid brittle CSS selectors. Target stability ↑ on dynamic pages, and stale-ref detection (§8.3) becomes possible. (Alternative = selectors: simple but fragile.)
- **Concurrency**: on a single tab, nearly everything must be sequential (even reads risk racing with mutations). The conservative `is_concurrency_safe` default `False` already behaves correctly, so leave it as-is ([02-tool-orchestration](../architecture/02-tool-orchestration.md)).

### 7.4 Batch Tool (Granularity C)

- `browser_run_steps([...])` — for deterministic, predictable sequences. The extension executes sequentially and aggregates. Stop · guard rules in §8.4.

### 7.5 Macros (A Few)

- `browser_fill_form · browser_extract · browser_wait_for …` — client routines whose steps are *built into* the extension. Saves turns on common flows.

---

## 8. Observation & Result Interpretation

The section tied directly to A's strength (adapting to the dynamic web). Covers **what to send as observation, and how to respond when a result deviates from expectations**.

### 8.1 Observation Representation

- **Default = semantic snapshot** — the accessibility tree (CDP `Accessibility.getFullAXTree`) or a cleaned DOM (script/style/invisible elements removed), **with stable refs assigned to interactive elements**. Small, LLM-friendly, and achieves the goal of "conveying the current page state".
- **raw HTML is optional** (`format:"html"`) — only when the true original is needed. Raw HTML of modern pages runs hundreds of KB\~MB, so it **immediately pressures the context window** → subtree/viewport scoping is required (ties to [04-context-compaction](../architecture/04-context-compaction.md)).
- **ref ↔ action coupling** — when observation assigns refs to elements, `click/type` take refs to avoid selector brittleness, and the stale-ref detection below becomes possible.

### 8.2 Three Result Classes

| Class | Meaning | Handling |
|---|---|---|
| ① Success as expected | op completed, state matches expectations | Proceed to the next step |
| ② **Succeeded but unexpected** | Transmission · execution went through, but the observed state diverges (unexpected navigation · modal · captcha · cookie banner · validation error · no effect) | **§8.3** |
| ③ Transport/execution failure | timeout · disconnect · op exception | Transport layer, §11 |

② is the "succeeded, yet surprising" case, so it is easy to miss — the focus of this section.

### 8.3 Handling the Unexpected (②) — The Agent Is the Handler

An unexpected result is **not a new control construct but information carried in the `tool_result`**. The LLM reads it on the next turn and replans, and the loop already supports this (tool_result → next turn → adapt). The design requirement therefore reduces to "make **results sufficiently observable** so the LLM can detect divergence". Two axes:

1. **Rich result payloads** — mutating ops also return post-execution signals (new URL · navigation occurred? · new dialog? · DOM changed significantly?) → not blind to side effects. (Or complement with A's `act → read` re-observation discipline.)
2. **Stale ref / missing element as a typed, recoverable result** — if a ref from a previous snapshot no longer matches the current DOM, the client must *not silently click the wrong element* but reply with a "ref stale" result → the agent re-observes. Key to correctness · safety.

### 8.4 Handling the Unexpected in C Batches

During a batch the agent is blind, so ② is more dangerous. The batch executor:

- **Stops at the first unexpected outcome** (no plow-ahead). Stop conditions = op failure / ref stale / unexpected navigation / dialog appears.
- Replies with `{completed:[…], stopped_at:k, reason, observation_at_stop}` — **including a snapshot at the stopping point** → the agent replans from the *actual state*.
- (Optional) **Guarded batch** — each step carries an expectation (e.g. URL match, selector appears) so the client detects divergence without the LLM and bails quickly. A middle ground that mitigates C's blindness.

---

## 9. Client Execution Mechanism

- **Recommended: map primitive ops to `chrome.debugger` (CDP)** — yields a computer-use-grade surface, from real input events · `Runtime.evaluate` · `Page.captureScreenshot` all the way to the accessibility tree. Observation refs are also assigned from AX tree/DOM nodes. The cost is the "debugging" banner and the `debugger` permission.
- **Mixing is possible**: cheap DOM reads via content-script; input · eval · screenshots via CDP. Macros are extension JS routines that compose primitives.

---

## 10. Session Routing & Transport

- **The extension opens a persistent WS to the server first.** The extension is behind NAT and cannot accept inbound connections, so client-initiated is the only direction that works. The server sends commands down that socket, and result frames come back up the same socket (JSON-RPC-over-WS pattern).
- **Registry**: `session_id ↔ connection` binding. The agent looks up its connection by its own session_id.
- **Correlation**: each command carries a `corr_id`, and responses are matched via `corr_id ↔ Future`.
- **Handshake**: on connect, authenticate with the user token, then bind the session. (Details unresolved — §14)

---

## 11. Integrity & Transport Failures (par-critical)

Networks are unreliable, so disconnects · timeouts are treated as the **normal path**. *(This section covers **transport-layer** failures. **Observational unexpected** outcomes, where transmission · execution succeeded but the observed state diverged, are covered by §8.3 — the two layers are distinct.)*

- **par-critical is preserved almost for free**: as long as `call()` returns *any* `ToolResult`, `run_tools` pairs up `tool_use`↔`tool_result`. Even if an exception leaks, the orchestrator catches it and `yield_missing_tool_result_blocks` (`core/loop.py`) backfills ([06-invariants](../architecture/06-invariants.md)). The bridge's only obligation is therefore to **never hang forever and convert every failure into an error `ToolResult`**.
- timeout → `ToolResult(is_error=True)`, disconnect → pending future reject → error result.
- **Idempotency pitfall**: even if the server gave up on a timeout, the client may have finished the command → retrying a mutating op risks **duplicate execution**. Mitigation: client-side corr_id dedup; mark mutating ops non-retryable.

---

## 12. Security Boundary

The extension executes server commands in the user's **logged-in** browser — a first-class trust surface.

- **Confirmation gate**: mutating actions (submit · payment · send) go through extension UI approval or an op/origin allowlist.
- **origin scoping**: restricts the sites the agent can drive.
- These are host policies, not a concern of the core loop.

---

## 13. Scope

| In scope | Out of scope (outside this proposal) |
|---|---|
| Remote tool seam · WS transport · session routing | Changes to `friday_agent/core` (all unmodified) |
| Hybrid tool surface (observation · primitives · macros) | Tool-boundary suspend/resume (stateless resume) |
| Stepwise (A) + linear batch (C) granularity | Programmatic batches with branching · waiting (code-as-action DSL) |
| Observation tools · ref targeting · observational-unexpected handling | A complete expectation language for guarded batches |
| par-critical transport failure mapping · security gate design | Concurrent multi-browser/multi-tab orchestration |

---

## 14. Open Questions

- **Observation representation tuning**: accessibility tree vs cleaned DOM, scope defaults, element ref lifetime · reuse policy.
- **Judging "unexpected"**: concrete rules for what counts as a surprise/batch stop condition (navigation · dialog · no effect, etc.).
- **Guarded-batch expectation language**: how much expressiveness to allow (linear guards vs branching · loops).
- **Authentication · pairing**: the concrete handshake for extension ↔ session binding (token issuance · verification · expiry).
- **Idempotency policy**: retry rules for mutating ops, client dedup retention period.
- **Macro catalog**: criteria for which flows to promote to macros, and the initial list.
- **Multi-tab/multi-browser**: routing · concurrency when one session handles multiple tabs.
- **suspend/resume migration conditions**: the triggers for moving to a tool-boundary suspend · resume model once the stateful server hits its limits (long-running actions · horizontal scaling · restart-durability requirements).
