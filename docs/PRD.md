# exactly-once — Product Requirements Document

*The effect-layer idempotency primitive for agent systems. Correctness first; tiny surface; honest guarantees.*

> Status: design PRD for v0.1–v0.3. Extends `SPEC.md`; requirements new or sharper than the spec are tagged **⊕ Beyond original spec**. Requirement IDs (`REQ-*`, `NFR-*`) are stable and referenced by `ARCHITECTURE.md` and `TEST-PLAN.md`.

---

## 1. Vision

> **Every agent that moves money, sends a message, or signs a transaction should be able to guarantee that effect fires once — in one import, without adopting a workflow engine.**

exactly-once is the smallest possible library that makes an unsafe retry safe. It is the executable form of the portfolio's trust thesis: *agents that act in the world must act exactly once.* It wins on being (a) correct, (b) honest about its limits, and (c) small enough to adopt in an afternoon.

## 2. Goals

| # | Goal |
|---|---|
| G1 | A `@once` decorator and a `with once(...)` context manager that dedupe a side-effecting call by idempotency key. |
| G2 | A minimal, pluggable **store interface** (`claim` / `commit` / `release`) with correct atomicity, and adapters for memory, SQLite, Redis, and Postgres. |
| G3 | **Crash-safety** (survives process death) and **replay-safety** (a re-run skips committed effects). |
| G4 | A **named, safe default** for the crash-mid-effect case: quarantine, never auto-refire. ⊕ |
| G5 | Framework-agnostic: works in raw loops, LangGraph, CrewAI, background jobs, webhook handlers. |
| G6 | An **onchain adapter** deduping by `(chain_id, from, nonce, calldata_hash)`. |
| G7 | Sync **and** async APIs with identical semantics. |
| G8 | A **TypeScript port** (v0.2) for the JS agent ecosystem. |
| G9 | Scrupulously honest, prominent documentation of the guarantee boundary. |
| G10 | A clean store/ledger interface that **stampede/costbomb can assert against** ("was this effect exactly-once after the crash?"). ⊕ |

## 3. Non-goals

| # | Non-goal | Because |
|---|---|---|
| NG1 | **Not a workflow engine.** No scheduling, timers, retries, orchestration, or durable control flow. | That is Temporal/Restate/DBOS/Inngest. exactly-once is a library that lives *inside* your existing orchestration. |
| NG2 | **Not distributed consensus.** No Paxos/Raft, no leader election, no cross-store coordination. | Guarantee strength is inherited from the store's atomicity, documented precisely. |
| NG3 | **Not a retry library.** | That's `tenacity`/`backoff` — exactly-once makes *their* retries safe. |
| NG4 | **Not an LLM cache / cost tool.** Wrapping pure generations for "dedupe" is misuse. | Non-effects gain no correctness from idempotency; cost-caching is a different product. |
| NG5 | **Not a guarantee that the *provider* ran once.** | We control the call site; end-to-end safety comes from composing with the provider's own idempotency key. |
| NG6 | **No auto-refire of effects with unknown outcomes, ever, by default.** | A half-completed payment must never silently re-fire. |

## 4. Personas

See `RESEARCH.md §3`. Primary target is **P1 — agent builder with real side-effects**; portfolio dogfooders **P2 (stampede/costbomb)** and **P3 (Cairn)** are the seeding channel; **P4 (backend engineer, no framework)** widens the market beyond agents; **P5 (onchain engineer)** is the brand-defining premium case.

---

## 5. Functional requirements

### 5.1 Key engine

| ID | Requirement | Priority |
|---|---|---|
| REQ-K1 | Accept a **user-supplied key**: a static string, or a callable receiving the wrapped call's args/kwargs and returning a string. | v0.1 must |
| REQ-K2 | Provide **derived keys** when none is supplied: a deterministic hash of `(callable qualified name, normalized args)`. | v0.1 must |
| REQ-K3 | **Argument normalization** for derived keys: order-insensitive kwargs, stable serialization, documented handling of unhashable/unstable inputs (e.g., raise rather than silently mis-key). | v0.1 must |
| REQ-K4 | **Payload fingerprinting** (Stripe/Powertools "validation"): if the same key arrives with different fingerprinted args, raise `KeyReuseError` rather than replay a mismatched result. | v0.1 should |
| REQ-K5 | **Key namespacing**: an optional prefix/namespace so distinct effects can't collide, and a documented policy on whether a code/version fingerprint is included. ⊕ | v0.1 should |
| REQ-K6 | Guidance + lint-in-docs against the **value-in-key anti-pattern** (key on business identity, not mutable amount). ⊕ | v0.1 must (docs) |

### 5.2 Store interface

| ID | Requirement | Priority |
|---|---|---|
| REQ-S1 | Define a minimal store protocol: **`claim(key) -> ClaimResult{FRESH \| IN_FLIGHT \| COMMITTED, result?}`**, **`commit(key, result)`**, **`release(key)`**. | v0.1 must |
| REQ-S2 | `claim` MUST be **atomic check-and-set**: only one caller can transition a key `FRESH → IN_FLIGHT`; all others see `IN_FLIGHT` or `COMMITTED`. | v0.1 must |
| REQ-S3 | Each adapter MUST **document its atomicity guarantee** and the writer model it supports (single-process / single-writer / multi-writer). | v0.1 must |
| REQ-S4 | Adapters: **memory** (single-process), **SQLite** (single-host, durable), **Redis** (`SET NX`), **Postgres** (`SERIALIZABLE` / unique-constraint upsert, multi-writer). | v0.1: memory+SQLite+Redis; Postgres: v0.1 should |
| REQ-S5 | `release` semantics MUST be **narrow and safe**: legal only when the effect is known *not* to have fired (pre-effect validation failure). Releasing after the effect boundary is forbidden and must be structurally hard to do by accident. ⊕ | v0.1 must |
| REQ-S6 | **Result codec**: results are serialized via a pluggable codec (JSON default). Non-serializable results raise a clear error; a documented "store a reference, not the payload" pattern is provided. ⊕ | v0.1 must |
| REQ-S7 | **Store-unavailable policy**: configurable fail-closed (default — don't run the effect) vs. fail-open. ⊕ | v0.1 must |
| REQ-S8 | Optional **committed-key TTL** for GC, with a loud caveat that an expired committed key is a re-fireable effect. | v0.1 should |
| REQ-S9 | An **inspectable ledger**: enumerate keys by state (for quarantine review and for stampede/costbomb assertions). ⊕ | v0.1 must |

### 5.3 Decorator & context manager

| ID | Requirement | Priority |
|---|---|---|
| REQ-A1 | `@once(store, key=...)` wraps a function so it executes at most once per key and replays the committed result thereafter. | v0.1 must |
| REQ-A2 | `with once(store, key=...) as guard:` exposes `guard.fresh` (bool), `guard.key`, `guard.result` (committed value on replay), and records commit on clean block exit. | v0.1 must |
| REQ-A3 | The context-manager form MUST **commit on successful exit** and **NOT commit on exception** (leaving the key in-flight → quarantine per policy), matching the decorator. | v0.1 must |
| REQ-A4 | Both forms expose the resolved **key** and the effect **outcome/state** for logging and assertions. | v0.1 must |
| REQ-A5 | `guard.key` MUST be retrievable **before** the effect runs, to pass through to a provider idempotency key (e.g., Stripe). ⊕ | v0.1 must |

### 5.4 Crash / replay safety

| ID | Requirement | Priority |
|---|---|---|
| REQ-C1 | A committed key MUST survive process death for durable stores (SQLite/Redis/Postgres); a re-run returns the committed result without re-executing. | v0.1 must |
| REQ-C2 | A concurrent second caller of an `IN_FLIGHT` key MUST be blocked or denied per policy — never run the effect in parallel. | v0.1 must |
| REQ-C3 | A crash **mid-effect** (after claim, before commit) MUST leave the key `IN_FLIGHT` and route it to the configured reconciliation policy on resume; the default policy MUST NOT re-execute. | v0.1 must |
| REQ-C4 | `IN_FLIGHT` keys MUST NOT silently auto-expire into re-runnable state for irreversible effects; any lease/TTL on `IN_FLIGHT` is explicit opt-in per policy. ⊕ | v0.1 must |

### 5.5 Reconciliation policies

| ID | Requirement | Priority |
|---|---|---|
| REQ-R1 | Pluggable reconciliation policy for the crash-mid-effect (`IN_FLIGHT`-on-resume) case. | v0.1 must |
| REQ-R2 | **Default policy = quarantine**: leave in-flight, surface for human/programmatic review, never auto-refire. | v0.1 must |
| REQ-R3 | Built-in alternative policies: `auto_retry` (for genuinely safe effects like idempotent emails), `fail` (raise), `check_then_decide` (run a user-supplied prober, e.g., "did the tx get mined?"). ⊕ | v0.1: quarantine+fail; others v0.2/v0.3 |
| REQ-R4 | Quarantined entries are enumerable and resolvable via the ledger (REQ-S9): mark-committed, mark-released, or force-refire (explicit, logged, human-triggered). ⊕ | v0.1 should |

### 5.6 Onchain adapter (v0.2)

| ID | Requirement | Priority |
|---|---|---|
| REQ-O1 | Derive keys as `(chain_id, from_address, nonce, calldata_hash)`. | v0.2 must |
| REQ-O2 | On resume, **check chain/mempool state** before signing; replay the known tx hash instead of re-signing at a new nonce. | v0.2 must |
| REQ-O3 | Integrate with common signers (web3.py; ethers.js in the TS port) without owning nonce management, but composing safely with a nonce manager. | v0.2 should |
| REQ-O4 | Treat mempool-pending as the onchain flavor of `IN_FLIGHT`; quarantine if chain state is indeterminate. | v0.2 must |

---

## 6. Non-functional requirements

| ID | Requirement |
|---|---|
| NFR-1 | **Correctness over everything.** Where availability and safety conflict, default to safety (fail-closed). Correctness is verified by property-based + crash-injection tests (see `TEST-PLAN.md`), not just examples. |
| NFR-2 | **Tiny surface.** Public API ≈ `once`, `Store`, `ClaimResult`, policies. If it needs a tutorial longer than the quickstart, it's too big. |
| NFR-3 | **Sync + async parity.** Identical semantics; async context manager (`async with once(...)`) and async-callable support; documented behavior across `await` boundaries. |
| NFR-4 | **Minimal deps.** Core has **zero required heavy deps**; store adapters are optional extras (`exactly-once[redis]`, `[postgres]`, `[onchain]`). Python 3.11+. |
| NFR-5 | **Honest guarantee boundaries.** The README and docs lead with a "Guarantees & Limits" section; no doc uses "exactly-once" without the "effect, not delivery" scoping nearby. |
| NFR-6 | **Zero LLM.** Pure infrastructure; no model calls, no model costs, works offline, deterministic. |
| NFR-7 | **Observability.** Every claim/commit/release emits a structured, optionally-traced event (trace-format compatible where the portfolio shares it) for the ledger and for stampede assertions. |
| NFR-8 | **Performance.** Overhead per guarded call dominated by one store round-trip; memory store adds negligible latency; documented big-O and a benchmark. |
| NFR-9 | **Portability.** MIT-licensed; typed (ships `py.typed`); no vendor lock-in in the core (adapters isolate vendor SDKs). |

---

## 7. Complete feature set by tier

| Tier | Feature | Reqs | Notes |
|---|---|---|---|
| **v0.1 — the primitive** | `@once` decorator + `with once` CM | REQ-A* | The two-line API |
| | Key engine (user + derived + fingerprint + namespace) | REQ-K1–K6 | |
| | Store interface + memory/SQLite/Redis adapters | REQ-S1–S9 | Postgres if time permits |
| | Crash + replay safety | REQ-C1–C4 | |
| | Quarantine (default) + fail policies | REQ-R1–R2 | |
| | Inspectable ledger | REQ-S9, REQ-R4 | The stampede/costbomb assertion hook |
| | Sync + async parity | NFR-3 | |
| | "Guarantees & Limits" docs + payment/email/tx examples | NFR-5, REQ-K6 | Includes provider-key-passthrough pattern |
| **v0.2 — reach** | Onchain adapter (nonce + calldata-hash) | REQ-O1–O4 | Brand-defining |
| | **TypeScript port** | G8 | Vercel AI SDK + raw loops first ⊕ |
| | LangGraph / CrewAI integration helpers | G5 | Thin adapters, not forks ⊕ |
| | Postgres multi-writer adapter (if not in v0.1) | REQ-S4 | |
| | `auto_retry` + `check_then_decide` policies | REQ-R3 | |
| **v0.3 — operate** | Reconciliation **policy library** | REQ-R* | Named, composable policies ⊕ |
| | **Quarantine dashboard** (CLI/TUI, or stampede report-renderer panel) | REQ-R4 | ⊕ |
| | Framework-native middleware (FastAPI/webhook edge convenience) | — | Close the loop with the HTTP-middleware incumbents ⊕ |
| | Metrics/OTel exporter for the ledger | NFR-7 | ⊕ |

**⊕ Features beyond the original spec:** named quarantine default (G4/REQ-R2), inspectable ledger as an assertion API (G10/REQ-S9), store-unavailable fail-closed policy (REQ-S7), result codec + size handling (REQ-S6), narrow/safe `release` semantics (REQ-S5), payload fingerprinting (REQ-K4), key namespacing/versioning (REQ-K5), value-in-key anti-pattern guidance (REQ-K6), `check_then_decide`/`auto_retry` policies (REQ-R3), quarantine dashboard (v0.3), framework middleware (v0.3), provider-key passthrough as the headline payment pattern (REQ-A5).

---

## 8. Success metrics

| Metric | Target | Rationale |
|---|---|---|
| **North star: PyPI dependents** (repos with `exactly-once` in deps) | grows monotonically; it's a *dependency*, so dependents > stars | SPEC §1.5 |
| PyPI downloads/week | steady growth post-launch | adoption proxy |
| Portfolio dogfood adoption | stampede + costbomb + (Cairn) assert against it by their v0.1 | G10; makes the portfolio the reference implementation |
| "Standard answer" citation | referenced in ≥N public "how do I stop double-charging?" threads | SPEC §1.5 |
| Correctness incidents | **zero** reported double-fires attributable to a documented-supported configuration | the brand is correctness; this is the metric that matters most |
| Docs clarity | ≥1 external write-up correctly restates the guarantee boundary unprompted | proves the honesty landed |

---

## 9. Assumptions & constraints

- **A1.** Users can provide a durable, atomic store (SQLite file, Redis, or Postgres) for any guarantee beyond process-local. The memory store is for tests/dev only and is documented as such.
- **A2.** The wrapped effect either accepts an idempotency key to pass through (best) or is otherwise the sole thing that call site does (so commit-on-exit maps to effect-succeeded). Effects that do *two* independent irreversible things in one guarded block are an anti-pattern; docs must say so.
- **A3.** For irreversible effects, users accept that crash-mid-effect → quarantine → human/prober decision is the correct behavior, not a bug.
- **C1.** MIT license; Python 3.11+ core; TS port targets modern Node/Deno.
- **C2.** No LLM, no network beyond the configured store and (for the onchain adapter) the chain RPC.
- **C3.** The store/ledger interface is a **stable public contract** because stampede/costbomb assert against it — breaking it is a breaking change for the portfolio.
