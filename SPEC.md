# exactly-once — Design Specification & PRD
### Idempotency middleware for agent side-effects
*The tiny library everyone embeds · v1.0 spec*

> **exactly-once** — the primitive agent frameworks forgot. Wrap any tool call that must never fire twice — a payment, an email, an onchain transaction — and exactly-once guarantees it runs a single time, even across retries, crashes, and replays.

---

## 1. PRD

### 1.1 Problem

Agents retry. They crash and resume. They get replayed during debugging. Every one of those behaviors can cause a side-effect to fire *twice* — a card charged twice, an email sent twice, a transaction submitted twice. In agent systems that touch money, duplicate side-effects are the difference between a bug and a lawsuit. Frameworks give you retries and checkpoints but *not* idempotency for the effects those retries cause. Developers hand-roll dedupe logic, badly, every time.

### 1.2 Why it wins

- **Perfect name, universal need, tiny surface.** Small, sharply-named, single-purpose libraries with a real guarantee spread far (think `tenacity`, `backoff`).
- **The keystone of the trust thesis in one import.** "Agents that move money must act exactly once" — this library *is* that sentence, executable.
- **Zero-LLM, zero-drama** — pure infrastructure; works forever, no model costs, trivial to adopt, easy to trust.
- **Cairn synergy:** exactly-once is the effect-layer companion to Cairn's state-recovery layer; stampede and costbomb both assert against it.

### 1.3 Users & JTBD

1. **Anyone building agents with real side-effects** — payments, email, messaging, onchain. (Primary.)
2. **stampede/Cairn users** — the thing chaos tests assert ("were side-effects exactly-once after the crash?").

### 1.4 Goals & non-goals

**Goals (v0.1):** a decorator + context manager that dedupes side-effecting calls via idempotency keys; pluggable stores (in-memory, SQLite, Redis); crash-safe (survives process death); replay-safe (a re-run skips already-committed effects); framework-agnostic (works in raw loops, LangGraph, CrewAI); onchain adapter for tx-dedupe.

**Non-goals:** being a full workflow engine (that's Temporal/Restate — exactly-once is a *library*, not infrastructure); distributed consensus (single-writer semantics, documented clearly).

### 1.5 Success metrics

- North star: downloads + dependents (it's a dependency, so PyPI dependents matter more than stars).
- Cited as the standard answer to "how do I stop my agent double-charging?"

---

## 2. ARCHITECTURE

### 2.1 The core guarantee & mechanism

Classic idempotency-key pattern, adapted for agent contexts:
1. Before a wrapped effect runs, compute a stable **idempotency key** (from tool name + normalized args, or user-supplied).
2. Atomically check-and-claim the key in the store. If already **committed**, return the stored result *without* re-running the effect. If **in-flight**, block/deny per policy (prevents double-fire under concurrency). If new, mark in-flight, run the effect, store the result, mark committed.
3. On crash mid-effect: the key is left in-flight; on resume, a reconciliation policy decides (safe-default: do not auto-retry effects of unknown outcome — surface for review, because a half-completed payment must never silently re-fire).

### 2.2 API sketch

```python
from exactly_once import once, Store

store = Store.sqlite("effects.db")

@once(store, key=lambda order, **_: f"charge:{order.id}")
def charge_card(order):
    return stripe.charge(order.customer, order.amount)   # runs at most once, ever

# context-manager form for inline effects
with once(store, key="send-welcome:user-4471") as guard:
    if guard.fresh:
        send_email(...)                       # skipped on any replay
```

> ⚠️ Key on stable business identity (`order_id`), never on a mutable value like amount — two distinct $50 charges must not collapse into one.

### 2.3 Components

- **Key engine:** deterministic key derivation (with normalization) + user override.
- **Store interface:** `claim(key) -> {fresh|in_flight|committed, result?}`, `commit(key, result)`, `release(key)`. Adapters: memory, SQLite, Redis, Postgres.
- **Onchain adapter:** dedupe by (nonce, calldata-hash); integrates with web3 signers so a resumed agent never double-submits a tx.
- **Reconciliation policies:** pluggable for the crash-during-effect case (default: quarantine, never auto-refire).

### 2.4 Tech stack

Python 3.11+ first (TypeScript port v0.2 — the JS agent ecosystem is large). Zero required heavy deps; async + sync APIs; store adapters optional extras. No LLM anywhere — it's plumbing.

### 2.5 Risks & mitigations

- **Distributed correctness claims** → be scrupulously honest about guarantees (single-writer strong; multi-writer needs a real transactional store); document the boundary — trust brand demands not overpromising.
- **"Temporal already does this"** → Temporal/Restate are workflow *infrastructure* you adopt wholesale; exactly-once is a two-line library you drop into an existing agent. Different weight class; name that explicitly.

---

## 3. ROADMAP

- **v0.1:** decorator + context manager; memory/SQLite/Redis stores; crash + replay safety; docs with the payment/email/tx examples.
- **v0.2:** onchain adapter; TypeScript port; LangGraph/CrewAI integration helpers.
- **v0.3:** reconciliation policy library; dashboards for quarantined effects.

## 4. LAUNCH

Essay: "Exactly-Once: The Primitive Agent Frameworks Forgot." Demo GIF: an agent crashes mid-payment, resumes, and does *not* double-charge — with and without exactly-once, side by side. Because it's a dependency, seed it by using it inside stampede, Cairn, and costbomb, so the portfolio itself is the first adopter and the reference implementation.
