# exactly-once — RESEARCH

*Problem sharpening, 2026 landscape, prior art, and gap analysis for the effect-layer idempotency primitive.*

> Companion to `SPEC.md`. This document does not restate the spec — it pressure-tests it, positions it in the live 2026 landscape, and marks where the design must be more honest or more ambitious than the original. Extensions to the spec are tagged **⊕ Beyond original spec**.

---

## 1. Thesis, sharpened

The spec's one-liner — *"idempotency middleware for agent side-effects"* — is correct but under-specifies the hardest and most important word in the name. So state it precisely:

> **exactly-once guarantees that a wrapped, side-effecting call is *entered* at most once per idempotency key, and that its committed result is *replayed* rather than re-executed on every subsequent retry, crash-resume, or replay — for as long as the backing store is durable and atomic.**

Three deliberate words in that sentence carry the entire trust brand:

1. **"at most once"** — not "exactly once". Under a crash in the microscopic window between "effect fired" and "result committed", the honest outcome is *unknown*, and the safe action is to run **zero** more times and escalate to a human. "At most once" + "replay on success" is the achievable composition; "exactly once, always, automatically" is a lie the field has been telling since Kafka 0.11 ([Brave New Geek — *You Cannot Have Exactly-Once Delivery*](https://bravenewgeek.com/you-cannot-have-exactly-once-delivery/)).
2. **"entered"** — exactly-once controls the *call site*, not the *provider*. It cannot un-charge a card. It can only decide whether to *call* `stripe.charge` again. The strongest end-to-end guarantee comes from composing our key with the provider's own idempotency key (§4.1).
3. **"durable and atomic store"** — the guarantee is exactly as strong as the store's `claim` primitive. A memory store gives you process-local safety and nothing more; Postgres `SERIALIZABLE` gives you multi-writer safety. The library is a state machine; the store is the source of truth.

**Why this framing wins the argument.** The category is crowded with tools that overpromise "exactly-once" and quietly mean "at-least-once with retries" (that's `tenacity`/`backoff`) or "exactly-once *within our runtime, if you adopt our runtime wholesale*" (that's Temporal/Restate/DBOS). exactly-once occupies the empty square: **a two-line library that gives you the *idempotent-consumer* half of the equation without adopting a workflow engine.**

---

## 2. The 2026 landscape

### 2.1 The distinction that organizes everything: **library vs. infrastructure**

The single most important positioning axis. Everything that "does exactly-once" falls into one of two weight classes:

| | **Infrastructure** (you adopt it wholesale) | **Library** (you drop it into existing code) |
|---|---|---|
| Examples | Temporal, Restate, DBOS, Inngest, Kafka EOS | Stripe idempotency-key SDK usage, AWS Lambda Powertools Idempotency, **exactly-once** |
| Adoption cost | Rewrite your control flow as workflows/steps; run (or pay for) a server/cluster; a new deployment topology | `pip install`; wrap one function; point at a store you already run |
| What it owns | Orchestration, scheduling, retries, timers, state, *and* effect-dedupe | **Only** effect-dedupe |
| Failure blast radius | Whole app depends on the engine being up | One function; degrades to "run the effect" if the store is down (your choice of policy) |
| When it's right | You're building a long-running, multi-step, durable workflow from scratch | You have an agent loop today and one call in it must never double-fire |

exactly-once is unambiguously the **library** column. This is not a limitation to apologize for — it is the entire product. The pitch is: *"You already have LangGraph/CrewAI/a raw while-loop doing your orchestration and retries. You don't need a second orchestrator. You need the one guarantee they forgot: that the payment inside the retry doesn't fire twice."*

### 2.2 Durable-execution engines (the "Temporal already does this" rebuttal)

By 2026 the durable-execution category has three serious contenders plus Inngest, and each handles exactly-once *differently* — which is itself the proof that "exactly-once" is not one thing ([Dev Note — *Durable Execution 2026*](https://devstarsj.github.io/2026/04/03/durable-execution-temporal-restate-dbos-distributed-workflows-2026/); [Spheron — *AI Agent Workflow Orchestration 2026*](https://www.spheron.network/blog/ai-agent-workflow-orchestration-temporal-inngest-restate-gpu-cloud/)):

| Engine | Exactly-once mechanism | The catch (and the opening for exactly-once) |
|---|---|---|
| **Temporal** | Deterministic **replay of workflow decisions** is exactly-once. | **Activities** — the units that touch the outside world — are **at-least-once by default**; Temporal's own docs tell you activities that mutate external state *need idempotency keys*. **exactly-once is exactly that missing idempotency layer, usable without Temporal.** ([Kai Waehner](https://www.kai-waehner.de/blog/2025/06/05/the-rise-of-the-durable-execution-engine-temporal-restate-in-an-event-driven-architecture-apache-kafka/)) |
| **Restate** | Journals each `ctx.run()` before execution; on crash re-invokes and replays the journal, skipping already-run steps; serializes handlers per virtual-object key. | Elegant, but you must model your effect as a Restate handler and run Restate. Great if you're all-in; irrelevant to an existing agent that just needs one guarded call. |
| **DBOS** | **Transactional** exactly-once *when the step writes to the same Postgres that stores workflow state* — the write and the durability record commit together. | The strongest real guarantee here — and it only holds for effects that are *themselves Postgres writes*. A Stripe charge or an email is a **foreign** mutation; DBOS cannot make it transactional. ([tiarebalbi — *DBOS vs Temporal*](https://www.tiarebalbi.com/en/blog/dbos-vs-temporal-postgres-durable-execution)) |
| **Inngest** | Step memoization — captures the result of each step and replays it. | Same shape as Temporal activities: memoization is at-least-once execution + result caching. ([Inngest blog](https://www.inngest.com/blog/durable-execution-key-to-harnessing-ai-agents)) |

**The crisp positioning line:** *Every durable-execution engine, at its effect boundary, reduces to "at-least-once execution + idempotency keys for external mutations." exactly-once ships that reduction as a standalone library — so you get the correctness property at the boundary without adopting the runtime.*

### 2.3 The reference model: Stripe idempotency keys

Stripe is the canonical, industry-trusted implementation and the mental model most engineers already have ([Stripe — *Designing robust and predictable APIs with idempotency*](https://stripe.com/blog/idempotency); [Stripe API Reference](https://docs.stripe.com/api/idempotent_requests)):

- Client generates a unique key (V4 UUID recommended) and sends it in the `Idempotency-Key` header on mutating (`POST`/`DELETE`) requests.
- Server **saves the status code and body of the first response** for that key — success *or* failure — and returns it verbatim on every retry.
- **Parameter fingerprinting:** Stripe compares incoming request params to the original and errors if they differ, catching accidental key reuse. (Powertools calls this "payload validation"; we adopt it — §4.2.)
- **TTL:** keys expire after ~24h.

The lesson for exactly-once: *the key is the contract, the stored result is the payload, and mismatched-args-under-same-key must be a loud error, not a silent replay.*

### 2.4 The transactional outbox / inbox pattern

The outbox pattern is frequently miscited as an exactly-once mechanism. It is not ([AWS Prescriptive Guidance](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html); [NP Blog 2025](https://www.npiontko.pro/2025/05/19/outbox-pattern)):

- Outbox guarantees **at-least-once** publication of an event atomically with a DB write.
- **Exactly-once end-to-end still requires the consumer to dedupe** — the *Idempotent Consumer* pattern, usually an **inbox table** keyed by message ID.
- **exactly-once *is* the idempotent-consumer primitive**, generalized beyond message IDs to any side-effecting call. This is a clean adjacency to state in docs: "pair a transactional outbox on the write side with exactly-once on the effect side."

### 2.5 The Python field (direct prior art)

| Tool | What it is | Why it isn't exactly-once |
|---|---|---|
| **AWS Lambda Powertools — Idempotency** | The closest real prior art. `@idempotent` / `@idempotent_function` decorators; `INPROGRESS`/`COMPLETE` states; pluggable `BasePersistenceLayer` (DynamoDB, Redis); conditional-put for atomic claim; payload validation; in-progress expiry for timeouts. ([docs](https://docs.aws.amazon.com/powertools/python/latest/utilities/idempotency/)) | **AWS-shaped and — critically — on an unhandled exception it *deletes* the in-progress record so the effect re-runs.** That is correct for re-runnable work and **catastrophic for a half-completed payment.** exactly-once inverts this default: crash/unknown → **quarantine, never auto-refire** (§4.3). Also Lambda-centric, DynamoDB-first, not agent/onchain-aware. |
| `tenacity`, `backoff` | Retry libraries. | They *cause* the double-fire problem; they are the "retry" exactly-once makes safe. Natural composition partners, not competitors. |
| `django-idempotency-key`, `asgi-idempotency-header`, `idemptx` | HTTP-middleware idempotency for web frameworks (FastAPI/Starlette/Django). | Scoped to **HTTP request/response** at the web edge, keyed off a header. Do nothing for an in-process agent tool call, a background job, or an onchain tx. Wrong layer. |
| `pytest-idempotent` | Test helper asserting a function is idempotent. | Testing aid, not a runtime guarantee. |

**Finding:** there is a real, dominant HTTP-edge idempotency pattern and one strong-but-mis-defaulted general library (Powertools), but **no framework-agnostic, agent-and-onchain-aware, quarantine-by-default effect-idempotency library.** That is the gap.

### 2.6 The JS/TS field (for the v0.2 port)

- The **Vercel AI SDK** is the de-facto default for TS agent projects in 2026 (model-agnostic, reference implementation for tool calling / streaming). The port should target it and raw tool loops first.
- **Honesty note:** several 2026 blog posts surface npm/PyPI package names (`agentidemp`, `tool-side-effects-tag`, `tool-result-cache`, `llm-retry`) as if they were established libraries ([buildmvpfast](https://www.buildmvpfast.com/blog/idempotent-ai-agent-retry-safe-patterns-production-workflow-2026), [channel.tel](https://www.channel.tel/blog/idempotent-tool-calls-agent-retry-safety)). These appear to be AI-generated content with unverifiable/likely-nonexistent packages — **do not cite them as prior art or assume the niche is taken.** The verifiable state of the TS field is: idempotency is discussed as a *pattern* ("build idempotent tool calls," "semantic dedupe of tool calls at cosine > 0.9") but there is **no dominant TS library owning it.** The TS port has open white space, mirroring Python.

### 2.7 Onchain / web3

Ethereum already has a native idempotency primitive most agent builders mishandle ([QuickNode — *Managing Nonces*](https://www.quicknode.com/guides/ethereum-development/transactions/how-to-manage-nonces-with-ethereum-transactions); [thirdweb — *Manage Ethereum Nonces*](https://thirdweb.com/learn/guides/how-to-manage-ethereum-nonces-with-thirdweb); [Nethereum docs](https://docs.nethereum.com/en/latest/nethereum-managing-nonces/)):

- The **nonce** enforces per-account ordering and makes a signed tx un-replayable on-chain.
- A **replacement transaction** reuses the same nonce with ≥12.5% higher gas.
- **Concurrent processes sharing an account cause nonce collisions and duplicate/dropped txs** — precisely the agent-crash-resume scenario. Infra like thirdweb Engine solves it with a locked local nonce queue synced to chain.
- **Insight for the onchain adapter:** the *right* idempotency key onchain is `(chain_id, from_address, nonce, calldata_hash)`. Dedupe on this and a resumed agent that already broadcast a tx will *replay the tx hash* instead of signing a second tx at a new nonce. The subtlety: a tx can be in the mempool but not yet mined — that is the onchain flavor of "in-flight," and it maps cleanly onto our `IN_FLIGHT` state (§4.4).

---

## 3. Users & jobs-to-be-done

| # | Persona | Job-to-be-done | The one sentence that sells them |
|---|---|---|---|
| P1 | **Agent builder with real side-effects** (payments, email, messaging, onchain) — *primary* | "My agent retries and resumes; make sure the card gets charged once." | "Wrap the charge in `@once` and stop hand-rolling dedupe." |
| P2 | **stampede / costbomb author (portfolio, dogfood)** | "My chaos test needs to *assert* 'was this effect exactly-once after the crash?'" | "exactly-once exposes the ledger stampede reads to prove it." |
| P3 | **Cairn user** | "Cairn recovers my agent's *state*; what recovers my *effects*?" | "exactly-once is the effect-layer companion to Cairn's state layer." |
| P4 | **Backend/infra engineer NOT using an agent framework** | "I have an idempotent-consumer / webhook-handler / background job; I don't want Temporal." | "The idempotent-consumer pattern as a two-line decorator." |
| P5 | **Onchain / DeFi agent engineer** | "A resumed bot must never double-submit a tx." | "Dedupe by (nonce, calldata-hash); resume replays the tx hash." |

---

## 4. Comprehensive use-case catalog

For each: the key, the effect, the crash risk, and the recommended reconciliation policy.

### 4.1 Payments (the flagship)
- **Effect:** `stripe.charge`, PSP capture, payout, refund.
- **Key:** `charge:{customer}:{order_id}` (business identity, **not** amount alone — see anti-pattern below).
- **Crash risk:** highest stakes; double-charge = chargeback + lawsuit.
- **Best practice ⊕:** **pass exactly-once's key through as the provider's own idempotency key** (`stripe.charge(..., idempotency_key=guard.key)`). Now there are *two* independent dedupe layers: even if exactly-once quarantines and a human force-refires, Stripe still dedupes downstream. Belt and suspenders. This should be the headline pattern in docs.
- **Reconciliation on crash-mid-effect:** **quarantine**. Never auto-refire a payment of unknown outcome.

### 4.2 Email / notifications
- **Effect:** `send_email`, SMS, push.
- **Key:** `welcome-email:{user_id}` or `notify:{event_id}`.
- **Crash risk:** duplicate email = annoyance, not lawsuit → this is the one class where an *auto-retry* reconciliation policy is defensible (spam beats silence). Policy is per-effect, and this is why policies must be pluggable.

### 4.3 Messaging / webhooks / queue consumers
- **Effect:** post to Slack, publish to a queue, call a partner webhook.
- **Key:** upstream `message_id` / `event_id` (the idempotent-consumer / inbox pattern, §2.4).
- **Crash risk:** medium; usually the consumer is genuinely idempotent-safe to retry.

### 4.4 Onchain transactions
- **Effect:** sign + broadcast a tx.
- **Key:** `(chain_id, from, nonce, calldata_hash)`.
- **Crash risk:** double-submit at a new nonce = double-spend; the mempool "in-flight" window is real (§2.7).
- **Reconciliation:** on resume, **check chain state** (is the tx hash mined / in mempool?) before ever signing again. Quarantine if indeterminate.

### 4.5 Agent tool calls (general)
- **Effect:** any tool the agent invokes that mutates external state (create ticket, provision resource, place order).
- **Key:** `tool:{name}:{normalized_args_hash}` (derived) or explicit.
- **Crash risk:** varies; the library's job is to make the *default* safe and let the builder relax it per-tool.

### 4.6 LLM calls (explicit non-target, documented)
- Pure text generation is **not** a side effect and should **not** be wrapped for correctness (only, optionally, for cost-caching — out of scope; that's a different tool). Wrapping non-effects adds latency and a store dependency for no correctness gain. Docs must say this plainly to prevent misuse.

---

## 5. Gap analysis

### 5.1 Gaps in the field
1. **No agent-native, framework-agnostic effect-idempotency library.** HTTP-edge middleware exists; a general in-process primitive does not (Powertools is the exception, and it's AWS-shaped + wrongly-defaulted for irreversible effects).
2. **The crash-mid-effect case is universally hand-waved.** Everyone documents the happy path and the concurrent-duplicate path; almost nobody has a *named default policy* for "we crashed after firing, before committing." exactly-once makes **quarantine** a first-class, named, documented outcome. This is the intellectual contribution.
3. **No onchain-aware idempotency primitive** that understands nonce + mempool-in-flight.
4. **"Exactly-once" is a marketing word, not a scoped guarantee.** A library whose *entire brand* is honesty about the boundary is differentiated by candor alone.

### 5.2 Gaps / soft spots in the current SPEC (to fix in PRD/ARCH)
1. **The key anti-pattern.** SPEC's example `key=lambda customer, amount: f"charge:{customer}:{amount}"` is **subtly wrong**: two *legitimately distinct* $50 charges to the same customer collapse to one, and a price change to a retried order *forks* the key. Keys must encode **business identity** (`order_id`), not mutable value. **Must be corrected in docs** — it's the most likely footgun.
2. **`release()` is under-specified and dangerous.** SPEC lists `release(key)` in the store interface but never says who may call it or when. A naive `release` on error re-opens the door to double-fire (this is exactly the Powertools delete-on-exception trap). ARCH must define `release` narrowly: legal only for *effects known not to have fired* (pre-effect validation failure), never after the effect boundary is crossed.
3. **Result serialization & size are unaddressed.** Stored results must be serialized; large/opaque/non-serializable results (a live SDK object) can't round-trip. Need a documented codec + size ceiling + "store a reference, not the payload" guidance.
4. **In-flight expiry / stuck locks.** A crashed in-flight key with no expiry blocks that key **forever**. Powertools solves this with `in_progress_expiry`. SPEC has no TTL story → ARCH must add lease/expiry semantics, carefully (expiry that's too aggressive re-opens the double-fire window).
5. **Store-unavailable policy.** If the store is down, does the effect run (availability) or fail (safety)? SPEC is silent. Correctness-first default: **fail closed** (don't run the effect), with an opt-out.
6. **Key TTL vs. correctness.** SPEC has no expiry policy for committed keys; expiry is a correctness knob, not just GC (an expired committed key = a re-fireable effect).
7. **Async + sync parity** is asserted but the concurrency semantics (does the context manager work across `await` boundaries and event-loop hops?) need explicit definition.

---

## 6. Differentiation summary

| Axis | exactly-once | Temporal/Restate/DBOS | Powertools Idempotency | tenacity/backoff | HTTP-middleware libs |
|---|---|---|---|---|---|
| Weight class | **Library** | Infrastructure | Library (AWS) | Library | Library (web edge) |
| Adopt without rewriting control flow | ✅ | ❌ | ✅ (Lambda) | ✅ | ✅ (HTTP only) |
| Framework-agnostic (raw loop / LangGraph / CrewAI) | ✅ | partial | ❌ | ✅ | ❌ |
| Crash-mid-effect default | **quarantine** | replay/at-least-once | **delete + re-run** ⚠️ | n/a | varies |
| Onchain-aware | ✅ (v0.2) | ❌ | ❌ | ❌ | ❌ |
| Honest, scoped guarantee | ✅ (the brand) | "exactly-once" (scoped to runtime) | partial | n/a | partial |
| Runtime dependency to operate | a store you already run | a server/cluster | AWS + DynamoDB/Redis | none | your web app |

**One-line wedge:** *the idempotent-consumer / activity-idempotency primitive that Temporal tells you to write yourself — shipped as a two-line, framework-agnostic library that defaults to safe (quarantine) instead of dangerous (auto-refire).*

---

## 7. Open questions

1. **Default store-down policy:** fail-closed (safety) vs. fail-open (availability) as the *shipped* default? (Recommendation: fail-closed; make it a one-liner to flip.)
2. **In-flight lease TTL:** should there even *be* an automatic expiry on `IN_FLIGHT`, given that expiry re-opens the double-fire window? Or is quarantine-forever-until-human the only correct default, with lease-TTL an explicit opt-in? (Recommendation: no auto-expiry on IN_FLIGHT for irreversible effects; TTL opt-in per policy.)
3. **Result codec:** JSON-only (safe, portable) vs. pluggable (pickle for power users, with loud warnings)? (Recommendation: JSON default + pluggable codec.)
4. **Key namespacing across deploys:** should the key include a code/version fingerprint (like Powertools' function-qualified prefix) so a refactor doesn't accidentally alias two different effects? Trade-off vs. cross-deploy replay.
5. **Multi-writer story for Redis:** `SET NX` is atomic but Redis is not strictly linearizable under failover — do we document Redis as "strong single-instance, best-effort under Sentinel/Cluster," and reserve "true multi-writer" for Postgres `SERIALIZABLE`?
6. **TS port scope:** does v0.2 target the Vercel AI SDK's tool-call lifecycle specifically, or stay framework-agnostic like Python?
7. **Reconciliation dashboard (v0.3):** standalone TUI/CLI, or a panel that plugs into stampede's report-renderer (shared primitive)?

---

## Sources

- Stripe — *Designing robust and predictable APIs with idempotency*: https://stripe.com/blog/idempotency
- Stripe — *Idempotent requests* (API reference): https://docs.stripe.com/api/idempotent_requests
- brandur.org — *Implementing Stripe-like Idempotency Keys in Postgres*: https://brandur.org/idempotency-keys
- AWS Lambda Powertools (Python) — *Idempotency*: https://docs.aws.amazon.com/powertools/python/latest/utilities/idempotency/
- AWS Prescriptive Guidance — *Transactional outbox pattern*: https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html
- Brave New Geek — *You Cannot Have Exactly-Once Delivery*: https://bravenewgeek.com/you-cannot-have-exactly-once-delivery/
- Confluent — *Exactly-once Semantics are Possible: Here's How Kafka Does it*: https://www.confluent.io/blog/exactly-once-semantics-are-possible-heres-how-apache-kafka-does-it/
- Dev Note — *Durable Execution: Temporal, Restate, DBOS (2026)*: https://devstarsj.github.io/2026/04/03/durable-execution-temporal-restate-dbos-distributed-workflows-2026/
- tiarebalbi — *DBOS vs Temporal: Choosing Durable Execution in 2026*: https://www.tiarebalbi.com/en/blog/dbos-vs-temporal-postgres-durable-execution
- Spheron — *AI Agent Workflow Orchestration: Temporal, Inngest, Restate (2026)*: https://www.spheron.network/blog/ai-agent-workflow-orchestration-temporal-inngest-restate-gpu-cloud/
- Inngest — *Durable Execution: The Key to Harnessing AI Agents in Production*: https://www.inngest.com/blog/durable-execution-key-to-harnessing-ai-agents
- Kai Waehner — *The Rise of the Durable Execution Engine*: https://www.kai-waehner.de/blog/2025/06/05/the-rise-of-the-durable-execution-engine-temporal-restate-in-an-event-driven-architecture-apache-kafka/
- QuickNode — *How to Manage Nonces with Ethereum Transactions*: https://www.quicknode.com/guides/ethereum-development/transactions/how-to-manage-nonces-with-ethereum-transactions
- thirdweb — *How to Manage Ethereum Nonces*: https://thirdweb.com/learn/guides/how-to-manage-ethereum-nonces-with-thirdweb
- Nethereum — *Managing nonces*: https://docs.nethereum.com/en/latest/nethereum-managing-nonces/
- NP Blog — *Transactional Outbox Pattern: From Theory to Production (2025)*: https://www.npiontko.pro/2025/05/19/outbox-pattern
