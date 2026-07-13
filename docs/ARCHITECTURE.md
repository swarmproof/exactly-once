# exactly-once — ARCHITECTURE

*The mechanism, the state machine, the store contract, and — most importantly — the precise boundary of what is and isn't guaranteed.*

> Extends `SPEC.md §2`. Design decisions are recorded ADR-style with alternatives. The **Guarantees & Limits** section (§9) is the load-bearing part of this document — read it before trusting anything above it.

---

## 1. The core guarantee & mechanism

exactly-once is a **state machine over a durable, atomically-mutable key**. Every guarded effect maps to exactly one key whose lifecycle is:

```
                         claim(key)  [atomic check-and-set]
                              │
              ┌───────────────┼────────────────────────────┐
              ▼               ▼                             ▼
          FRESH          IN_FLIGHT                     COMMITTED
      (no record)     (record exists,               (record exists,
                       no result yet)                result stored)
```

The invariant the whole library defends:

> **At most one caller may observe `FRESH` for a given key.** Everyone else observes `IN_FLIGHT` or `COMMITTED`. The single `FRESH` observer — and only it — is permitted to run the effect.

Everything downstream (crash-safety, replay-safety, concurrency-safety) is a consequence of that one atomic transition being correct in the store.

### 1.1 The happy path (sequence)

```
caller                 exactly-once                 store                     effect (Stripe/email/tx)
  │  once(key)  ─────────▶  claim(key) ───────────────▶  CAS FRESH→IN_FLIGHT
  │                     ◀── FRESH (I won the claim) ◀──  ok
  │                        run effect ───────────────────────────────────────▶  charge()
  │                                              (passes guard.key as provider idempotency key)
  │                     ◀── result ◀─────────────────────────────────────────  ok
  │                        commit(key, result) ──────▶  IN_FLIGHT→COMMITTED (store result)
  │  ◀── result ◀──────────
```

### 1.2 The replay path

```
caller (re-run)          exactly-once                 store
  │  once(key)  ─────────▶  claim(key) ───────────────▶  read
  │                     ◀── COMMITTED + stored result ◀─
  │  ◀── result (effect NOT re-run) ◀──
```

### 1.3 The concurrent-duplicate path

```
worker A ─▶ claim(key) ─▶ CAS wins ─▶ FRESH  ─▶ runs effect ─▶ commit
worker B ─▶ claim(key) ─▶ CAS loses ─▶ IN_FLIGHT ─▶ blocked/denied per policy (never runs effect)
```

### 1.4 The crash-mid-effect path (the case that defines the brand)

```
worker ─▶ claim(key) ─▶ FRESH ─▶ run effect ──✗ CRASH (effect may or may not have committed)
                                      │
                                 key left IN_FLIGHT
                                      │
worker (resume) ─▶ claim(key) ─▶ IN_FLIGHT ─▶ reconciliation policy
                                      │
                          default: QUARANTINE (do not run; surface for review)
```

---

## 2. Full state diagram

```
                         ┌──────────────────────────────────────────────┐
                         │                                              │
                         │                (no record)                   │
                         │                  FRESH                       │
                         └───────────────────┬──────────────────────────┘
                                             │ claim(): atomic CAS, exactly one winner
                          winner │                                  │ loser
                                 ▼                                  ▼
              ┌────────────────────────────────┐        (observes IN_FLIGHT or COMMITTED)
              │            IN_FLIGHT            │                    │
              │  (record exists, no result)    │        ┌───────────┴───────────┐
              └───────┬───────────────┬────────┘        ▼                       ▼
                      │               │            IN_FLIGHT               COMMITTED
       effect ok      │               │ crash /    → block/deny/          → replay stored
       commit(result) │               │ exception    reconcile              result
                      ▼               ▼
           ┌────────────────┐   ┌───────────────────────────────────────────────┐
           │   COMMITTED    │   │      IN_FLIGHT (orphaned) → reconcile:          │
           │ (result stored,│   │  ┌──────────────┬───────────────┬───────────┐  │
           │  effect done)  │   │  │  QUARANTINE  │    fail       │ check_then│  │
           └───────┬────────┘   │  │  (default:   │  (raise)      │ _decide   │  │
                   │            │  │  never       │               │ (prober)  │  │
        release()  │            │  │  refire)     │               │           │  │
   (ONLY legal when│            │  └──────┬───────┴───────┬───────┴─────┬─────┘  │
    effect NOT     │            │         │ human/prober decides:       │        │
    fired — see    │            │         ▼               ▼             ▼        │
    §5 / ADR-004)  │            │   mark COMMITTED   mark released   force-refire │
                   ▼            │   (effect done)    (safe to retry) (explicit,   │
              (record GC'd      │                                     logged)     │
               or TTL'd)        └───────────────────────────────────────────────┘
```

States are intentionally only three (`FRESH`/`IN_FLIGHT`/`COMMITTED`), mirroring the minimal viable set (cf. Powertools' `INPROGRESS`/`COMPLETE`). "Quarantined" is not a fourth state — it is `IN_FLIGHT` that has been observed as orphaned and routed to a policy; keeping it as `IN_FLIGHT` is deliberate so that **the only way out is an explicit human/prober decision**, never a timer.

---

## 3. The store interface

The store is the source of truth. The library is stateless between calls. The entire correctness of exactly-once is delegated to `claim`'s atomicity.

```python
from typing import Protocol, Optional, Any
from enum import Enum

class State(Enum):
    FRESH = "fresh"
    IN_FLIGHT = "in_flight"
    COMMITTED = "committed"

class ClaimResult:
    state: State
    result: Optional[Any]      # populated iff COMMITTED
    key: str
    fingerprint: Optional[str] # for REQ-K4 payload validation

class Store(Protocol):
    def claim(self, key: str, *, fingerprint: str | None = None) -> ClaimResult:
        """ATOMIC check-and-set. If no record: create IN_FLIGHT, return FRESH.
        If IN_FLIGHT: return IN_FLIGHT. If COMMITTED: return COMMITTED + result.
        MUST be atomic: concurrent callers see exactly one FRESH."""

    def commit(self, key: str, result: bytes) -> None:
        """IN_FLIGHT -> COMMITTED, storing the serialized result. Idempotent."""

    def release(self, key: str) -> None:
        """IN_FLIGHT -> FRESH (delete record). ONLY legal pre-effect. See §5."""

    # ledger / reconciliation (REQ-S9)
    def get(self, key: str) -> ClaimResult | None: ...
    def list(self, state: State | None = None) -> Iterator[ClaimRecord]: ...
```

The async store is the same protocol with `async def`. `commit` is defined idempotent so a commit that itself is retried after a crash-just-before-return is safe.

### 3.1 Store adapters & their atomicity guarantees

The most important table in this document. **The guarantee you get is the guarantee of the row you pick.**

| Adapter | Atomic-claim mechanism | Durability | Writer model | Honest guarantee | Use for |
|---|---|---|---|---|---|
| **memory** | `dict` + a per-process lock/`asyncio.Lock` | none (RAM) | single **process** only | Strong **within one process**; nothing survives a crash. | tests, dev, single-process demos |
| **SQLite** | `INSERT` on a `PRIMARY KEY` inside a transaction (`INSERT OR ABORT`); `BEGIN IMMEDIATE` for writers | fsync-durable file | single **host**, effectively single-writer (SQLite serializes writers) | Strong on one host; concurrent processes on the same file are serialized by SQLite's write lock. **Not** for multi-host. | single-node agents, jobs, CI |
| **Redis** | `SET key value NX` (atomic set-if-not-exists); Lua script for CAS+read | AOF/RDB-durable (config-dependent) | multi-writer against **one** Redis | Strong against a single Redis instance. **Under Sentinel/Cluster failover Redis is not strictly linearizable** — a failover window can, in principle, lose a very-recent claim. Documented as "strong single-instance, best-effort under failover." | distributed workers sharing one Redis; the common production case |
| **Postgres** | `INSERT ... ON CONFLICT DO NOTHING` (unique key) returning inserted-or-not, inside `SERIALIZABLE`; serialization failures surface as claim contention | WAL-durable, replicated | **true multi-writer** | The strongest offered. `SERIALIZABLE` + unique constraint gives linearizable claim across many writers/hosts. Serialization failures → retry the *claim* (safe; effect hasn't run). | multi-host production; the "I need real multi-writer" answer |

**Design rule (ADR-002):** an adapter that cannot make `claim` atomic is not a valid adapter. There is no "eventually consistent" store option, because an eventually-consistent claim is a double-fire waiting to happen.

---

## 4. Key derivation & normalization

Priority order for the key of a guarded call:

1. **Explicit static key** — `once(store, key="welcome:user-4471")`. Highest trust; user owns identity.
2. **Explicit callable key** — `once(store, key=lambda order, **_: f"charge:{order.id}")`. Derives from *business identity*.
3. **Derived key** (fallback) — `hash(qualified_callable_name + normalize(args, kwargs))`.

### 4.1 Normalization (REQ-K3)
- kwargs sorted by name; args positional; values serialized with the result codec (JSON default).
- Unhashable/unstable inputs (open sockets, live objects, `datetime.now()`) → **raise `UnstableKeyError`** rather than silently mint a per-call key. Silent mis-keying is the worst failure mode (looks like it works, dedupes nothing).
- Optional namespace/prefix (REQ-K5) prepended: `{namespace}:{key}`.

### 4.2 Payload fingerprinting (REQ-K4)
Independently of the key, an optional **fingerprint** of chosen fields is stored on first claim. If a later `claim` presents the same key with a different fingerprint → `KeyReuseError`. This catches "same key, different args" (Stripe's parameter-mismatch check; Powertools' payload validation).

### 4.3 The anti-pattern, called out in code and docs (REQ-K6)
`key=f"charge:{customer}:{amount}"` (from the original SPEC sketch) is **wrong**: it keys on a *mutable value*. Two legitimate distinct $50 charges collapse into one; a repriced retry forks the key. The library's docstrings and docs use `key=f"charge:{order_id}"` and explain why. Business identity, never mutable value.

---

## 5. Crash-mid-effect reconciliation

The unsolvable-alone case, handled honestly.

**The physics:** between "effect fired" and "commit landed" there is a window. If the process dies in that window, the store shows `IN_FLIGHT` and *the library cannot know whether the effect happened.* No amount of cleverness in a single-store library closes this window — it is the Two Generals problem at the effect boundary.

**The response — three layers:**

1. **Default policy: `quarantine`.** On observing an orphaned `IN_FLIGHT` at resume, do **not** run the effect. Record it in the ledger as needs-review. Return control per config (raise `QuarantinedError` or return a sentinel). A human or a prober resolves it. This is the safe default because *re-running an unknown-outcome payment is strictly worse than pausing it.*
2. **`check_then_decide` policy (opt-in).** Run a user-supplied prober that *observes the world* (query Stripe by idempotency key; query chain for the tx hash) and returns `committed` / `not_committed` / `unknown`. `committed` → mark COMMITTED (replay). `not_committed` → `release` + re-run. `unknown` → quarantine. This is how you *narrow* the window using external truth.
3. **Provider-key passthrough (the real fix, ADR-005).** Pass `guard.key` into the effect as the provider's own idempotency key. Now even a human-forced refire is deduped by Stripe/the chain downstream. exactly-once's job shrinks to "don't refire without a decision"; the provider's job is "don't double-apply even if you do." Belt and suspenders — documented as the recommended production pattern for money-movement.

**`release` safety (ADR-004):** `release` (IN_FLIGHT→FRESH) is only ever called by exactly-once itself in the pre-effect window (e.g., a fingerprint/validation failure *before* the effect runs) or by an explicit human/prober decision after a `not_committed` verdict. It is **never** called automatically on an exception after the effect boundary — that is precisely the Powertools delete-on-exception behavior that is unsafe for irreversible effects. The context-manager and decorator are structured so ordinary effect exceptions do **not** trigger `release`; they leave the key `IN_FLIGHT` for reconciliation.

---

## 6. Onchain adapter (v0.2)

```python
from exactly_once.onchain import onchain_once

@onchain_once(store, signer=signer, rpc=rpc)   # key = (chain_id, from, nonce, calldata_hash)
def send_payout(to, amount):
    return signer.send_transaction({...})
```

- **Key:** `(chain_id, from_address, nonce, calldata_hash)`. The nonce is the chain's own idempotency token; combining it with calldata-hash detects "same intent, same slot."
- **In-flight = mempool-pending.** A broadcast-but-unmined tx is the onchain `IN_FLIGHT`. On resume, the adapter's prober queries chain + mempool for the tx hash before signing anything.
- **Reconciliation:** `check_then_decide` with a chain prober is the natural default here (chain state *is* observable, unlike a fire-and-forget email) — mined → COMMITTED (replay hash); dropped → `release` + resign at the correct nonce; pending/indeterminate → quarantine.
- **Composition, not ownership:** the adapter does not replace a nonce manager (thirdweb-Engine-style locked queue); it composes with one, adding the crash/replay dedupe layer on top.

---

## 7. Concurrency model — the boundary, stated precisely

| Scenario | Safe? | Why |
|---|---|---|
| Single process, sync, retries in a loop (memory/SQLite) | ✅ strong | one atomic claim owner; process-local lock or SQLite write-lock |
| Single process, async, concurrent tasks (memory) | ✅ strong | `asyncio.Lock` around claim; single event loop |
| Multiple processes, one host (SQLite) | ✅ strong | SQLite serializes writers via file lock |
| Multiple processes/hosts (Redis, single instance) | ✅ strong (single-instance) | `SET NX` is atomic on one Redis |
| Multiple processes/hosts (Redis under Sentinel/Cluster failover) | ⚠️ best-effort | Redis not strictly linearizable across failover; a failover-window claim can be lost |
| Multiple processes/hosts (Postgres `SERIALIZABLE`) | ✅ strong (true multi-writer) | linearizable claim via unique constraint + serializable isolation |
| Two *different* stores for the same key | ❌ unsafe | no cross-store coordination; NG2. One key → one store. |

**The one-sentence boundary:** *single-writer (or single atomic store) is strong; "multi-writer" is exactly as strong as the store you chose — use Postgres `SERIALIZABLE` when you mean it, Redis when single-instance is acceptable, and never split one key across two stores.*

---

## 8. API surface

### 8.1 Python

```python
from exactly_once import once, Store, State
from exactly_once.policies import quarantine, fail, check_then_decide

store = Store.sqlite("effects.db")                 # or .memory(), .redis(url), .postgres(dsn)

# decorator
@once(store, key=lambda order, **_: f"charge:{order.id}",
      policy=quarantine, on_store_down="fail")     # fail-closed default
def charge_card(order):
    return stripe.charge(order.customer, order.amount,
                         idempotency_key=charge_card.last_key)  # passthrough (ADR-005)

# context manager (sync + async)
with once(store, key="send-welcome:user-4471") as guard:
    if guard.fresh:
        send_email(...)          # skipped on any replay; committed on clean exit
    else:
        use(guard.result)

async with once(store, key=f"notify:{event_id}") as guard:
    if guard.fresh:
        await post_to_slack(...)

# ledger / reconciliation
for rec in store.list(state=State.IN_FLIGHT):      # quarantine review
    ...
```

### 8.2 TypeScript port (v0.2) — shape

```ts
import { once, Store } from "exactly-once";

const store = Store.sqlite("effects.db"); // .memory() | .redis(url) | .postgres(dsn)

// wrapper (works with Vercel AI SDK tool lifecycle + raw loops)
const chargeCard = once(store, { key: (o) => `charge:${o.id}`, policy: "quarantine" },
  async (o) => stripe.charge(o.customer, o.amount, { idempotencyKey: `charge:${o.id}` }));

// scoped form
await once(store, { key: `send-welcome:${userId}` }, async (guard) => {
  if (guard.fresh) await sendEmail(...);
});
```

Semantics are identical to Python; the store contract is the shared cross-language spec (so a Python producer and a TS consumer could, in principle, share a Postgres ledger).

---

## 9. Guarantees & Limits (READ THIS)

### 9.1 What exactly-once **guarantees**
- **G-1.** Given a store with an atomic `claim`, the wrapped effect is **entered at most once per key** across retries, concurrent workers, crashes, and replays.
- **G-2.** After a successful `commit`, every subsequent call with the same key **replays the stored result and does not re-execute the effect** — for as long as the committed record is durable and un-expired.
- **G-3.** A concurrent second caller of an `IN_FLIGHT` key **never runs the effect in parallel** (blocked/denied per policy).
- **G-4.** A crash mid-effect **never results in an automatic re-fire** under the default policy; the key is quarantined for an explicit decision.

### 9.2 What exactly-once **does NOT guarantee**
- **L-1. It is not "exactly-once *delivery*."** True exactly-once delivery is impossible (Two Generals / FLP). This is "at-most-once *execution* + replay-on-success" = the achievable **exactly-once *effect*** — and only when composed with an idempotent provider does end-to-end "the world changed once" hold.
- **L-2. It cannot know the outcome of a crash-mid-effect.** It can only refuse to guess. Narrowing that window requires a prober (`check_then_decide`) or a provider idempotency key (passthrough) — both user-supplied.
- **L-3. It is only as strong as the store.** memory = single-process; SQLite = single-host; Redis = single-instance-strong / failover-best-effort; Postgres `SERIALIZABLE` = true multi-writer. Pick deliberately.
- **L-4. It does not manage retries, timers, or orchestration.** Not a workflow engine (NG1).
- **L-5. It does not make a non-idempotent provider idempotent by itself.** It controls the call site, not the provider. Compose with the provider's idempotency key for end-to-end safety.
- **L-6. It provides no cross-store or cross-key transaction.** One key → one store → one effect. Guarding two independent irreversible effects in one block is unsupported and unsafe.
- **L-7. Expiry is a correctness knob.** An expired committed key is a re-fireable effect; an auto-expiring `IN_FLIGHT` re-opens the double-fire window. Defaults favor safety (no `IN_FLIGHT` auto-expiry for irreversible effects).

---

## 10. ADRs

### ADR-001 — Library, not infrastructure
**Decision:** ship as an embeddable library with a pluggable store, not a server/runtime.
**Alternatives:** (a) a sidecar service with an API; (b) a full durable-execution runtime.
**Why:** the entire market opening is "I don't want to adopt Temporal for one guarded call" (RESEARCH §2.1–2.2). A server reintroduces the adoption cost we're differentiating against. **Consequence:** we inherit the store's guarantees rather than providing our own consensus (NG2).

### ADR-002 — Correctness lives in the store's atomic `claim`
**Decision:** the library is stateless; `claim` must be an atomic check-and-set, and any adapter that can't provide it is invalid.
**Alternatives:** library-side locking/coordination across stores.
**Why:** cross-store coordination is distributed consensus, explicitly a non-goal. Delegating to a single atomic primitive per store keeps the library tiny and the guarantees auditable. **Consequence:** the adapter table (§3.1) *is* the guarantee spec.

### ADR-003 — Quarantine is the default reconciliation policy
**Decision:** crash-mid-effect → do not re-run; surface for review.
**Alternatives:** (a) auto-retry (Powertools-style delete-and-rerun); (b) fail-and-crash.
**Why:** for irreversible effects, an automatic re-fire of unknown outcome is the worst possible action. Safe-by-default is the brand. Auto-retry remains available *opt-in* for genuinely safe effects (emails). **Consequence:** users of irreversible effects must build a review/prober path — which is correct, not a burden.

### ADR-004 — `release` is narrow and pre-effect only
**Decision:** `release` (IN_FLIGHT→FRESH) is legal only before the effect boundary or after an explicit `not_committed` verdict; never auto-invoked on a post-effect exception.
**Alternatives:** delete-on-exception (Powertools default).
**Why:** delete-on-exception silently re-opens the double-fire window for exactly the effects (payments) where it's most dangerous. **Consequence:** an exception in the effect leaves the key `IN_FLIGHT` (quarantined), not `FRESH`.

### ADR-005 — Provider-key passthrough is the recommended money-movement pattern
**Decision:** document (and make ergonomic via `guard.key`) passing the exactly-once key as the provider's own idempotency key.
**Alternatives:** rely solely on our store.
**Why:** it composes two independent dedupe layers; even a human-forced refire is deduped downstream (RESEARCH §4.1). It's the difference between "at-most-once at our call site" and "the world changed once end-to-end." **Consequence:** for payments/onchain, docs treat this as mandatory, not optional.

### ADR-006 — Fail-closed on store-unavailable by default
**Decision:** if the store can't be reached, do **not** run the effect (raise); make fail-open a one-liner opt-in.
**Alternatives:** fail-open (run the effect for availability).
**Why:** running an unguarded effect defeats the entire purpose; a correctness-first library must default to safety. **Consequence:** exactly-once's availability is coupled to the store's — acceptable and documented (NFR-1).

### ADR-007 — Three states only; "quarantine" is observed-orphaned IN_FLIGHT
**Decision:** `FRESH`/`IN_FLIGHT`/`COMMITTED`; no dedicated quarantine state.
**Alternatives:** a fourth `QUARANTINED` state.
**Why:** keeping quarantine as `IN_FLIGHT` means the only exits are explicit decisions (mark-committed / release / force-refire) — a timer can never quietly resurrect it. Minimal state = minimal ways to be wrong. **Consequence:** the ledger distinguishes "recently in-flight" from "orphaned" by age/heartbeat metadata, not by a distinct state.
