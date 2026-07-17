# exactly-once

### Idempotency middleware for agent side-effects

> The primitive agent frameworks forgot. Wrap any tool call that must never fire twice — a payment, an email, an onchain transaction — and **exactly-once** guarantees it runs a single time, even across retries, crashes, and replays.

<!-- TODO: demo GIF — an agent crashing mid-payment, resuming, and NOT double-charging -->
<p align="center"><em>▶ demo GIF coming — agent crashes mid-payment, resumes, does not double-charge (with vs without)</em></p>

> **Status:** 🚧 v0.1 in progress. Zero-LLM, tiny surface, drop-in.

---

## Why

Agents retry. They crash and resume. They get replayed during debugging. Every one of those can fire a side-effect *twice* — a card charged twice, an email sent twice, a transaction submitted twice. In systems that touch money, duplicate side-effects are the difference between a bug and a lawsuit. Frameworks give you retries and checkpoints but **not idempotency for the effects those retries cause.**

## Install

```bash
pip install exactly-once            # core, zero heavy deps
pip install "exactly-once[redis]"   # + Redis store   (also: [postgres], [onchain])
```

## Quickstart

```python
from exactly_once import once, Store, current_key

store = Store.sqlite("effects.db")   # or .memory() / .redis(url) / .postgres(dsn)

@once(store, key=lambda order, **_: f"charge:{order.id}")
def charge_card(order):
    # pass our key through as Stripe's own idempotency key — belt and suspenders
    return stripe.charge(order.customer, order.amount, idempotency_key=current_key())

charge_card(order)   # runs the charge
charge_card(order)   # replays the stored result — Stripe is NOT called again

# context-manager form for an inline effect
with once(store, key="send-welcome:user-4471") as guard:
    if guard.fresh:
        guard.result = send_email(...)        # skipped on any replay
```

> ⚠️ Key on stable business identity (`order_id`), never on a mutable value like amount — two distinct $50 charges must not collapse into one.

**See it prevent a double-charge in 15 seconds:**

```bash
python examples/crash_mid_payment.py    # agent crashes mid-payment, with vs without
```

More runnable examples in [`examples/`](./examples/).

## The guarantee (honestly scoped)

Classic idempotency-key pattern adapted for agents: compute a stable key → atomically check-and-claim → if **committed**, return the stored result without re-running; if **in-flight**, block/deny per policy; if new, run and commit. On a crash mid-effect the key is left in-flight and **quarantined** — a half-completed payment must never silently re-fire. Single-writer semantics are strong; multi-writer needs a real transactional store (documented, not overpromised).

Pluggable stores: memory · SQLite · Redis · Postgres. Onchain adapter dedupes by (nonce, calldata-hash). See [`SPEC.md`](./SPEC.md) and [`ROADMAP.md`](./ROADMAP.md).

### What it guarantees — and what it doesn't

**Guarantees** (given a store with an atomic claim): the effect is *entered at most once per key* across retries, concurrent workers, crashes, and replays; after a commit every later call replays the stored result; a concurrent second caller never runs in parallel; a crash mid-effect never auto-re-fires.

**Does not**: it is exactly-once *effect* (at-most-once execution + replay-on-success), **not** exactly-once *delivery* — that's impossible (Two Generals / FLP). It can't know the outcome of a crash-mid-effect; it refuses to guess (quarantine) and lets a prober or a provider idempotency key narrow the window. It's only as strong as the store you pick:

| Store | Guarantee | Use for |
|---|---|---|
| memory | strong within one process | tests, dev |
| SQLite | strong on one host | single-node agents, jobs, CI |
| Redis | strong single-instance, best-effort under failover | distributed workers, one Redis |
| Postgres `SERIALIZABLE` | true multi-writer, linearizable | multi-host production |

Full boundary in [`docs/ARCHITECTURE.md` §9](./docs/ARCHITECTURE.md).

## Development

```bash
uv venv --python 3.11 && uv pip install -e ".[dev]"
uv run pytest                              # full suite (Redis/Postgres tests need Docker)
uv run mypy src/exactly_once               # strict typing
uv run ruff check src tests examples       # lint
uv run python scripts/check_docs_honesty.py  # the docs-honesty gate
```

## Part of the Swarm Proof toolkit

*Trust infrastructure for the agent economy — seven projects, one thesis.*

| Project | What it does |
|---------|--------------|
| [stampede](https://github.com/swarmproof/stampede) | Point a herd of realistic agents at your system before real ones arrive |
| [mockworld](https://github.com/swarmproof/mockworld) | A synthetic internet for agents — fake Stripe, Gmail, exchange, instantly |
| [mcp-probe](https://github.com/swarmproof/mcp-probe) | The CI quality suite for MCP servers — lint, contract-test, benchmark, load |
| [costbomb](https://github.com/swarmproof/costbomb) | Denial-of-wallet fuzzing — find the inputs that make your agent spend $500 |
| **exactly-once** ← *you are here* | Idempotency middleware so agent side-effects fire once |
| [agent-postmortems](https://github.com/swarmproof/agent-postmortems) | A structured incident database + post-mortem standard for agent failures |
| [awesome-agent-reliability](https://github.com/swarmproof/awesome-agent-reliability) | The curated map of the field |

## License

[MIT](./LICENSE). No LLM anywhere — it's plumbing. Citable via [`CITATION.cff`](./CITATION.cff).
