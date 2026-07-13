# exactly-once

### Idempotency middleware for agent side-effects

> The primitive agent frameworks forgot. Wrap any tool call that must never fire twice — a payment, an email, an onchain transaction — and **exactly-once** guarantees it runs a single time, even across retries, crashes, and replays.

<!-- TODO: demo GIF — an agent crashing mid-payment, resuming, and NOT double-charging -->
<p align="center"><em>▶ demo GIF coming — agent crashes mid-payment, resumes, does not double-charge (with vs without)</em></p>

> **Status:** 🚧 v0.1 in progress. Zero-LLM, tiny surface, drop-in.

---

## Why

Agents retry. They crash and resume. They get replayed during debugging. Every one of those can fire a side-effect *twice* — a card charged twice, an email sent twice, a transaction submitted twice. In systems that touch money, duplicate side-effects are the difference between a bug and a lawsuit. Frameworks give you retries and checkpoints but **not idempotency for the effects those retries cause.**

## Quickstart

```python
from exactly_once import once, Store

store = Store.sqlite("effects.db")

@once(store, key=lambda customer, amount: f"charge:{customer}:{amount}")
def charge_card(customer, amount):
    return stripe.charge(customer, amount)   # runs at most once, ever

with once(store, key="send-welcome:user-4471") as guard:
    if guard.fresh:
        send_email(...)                       # skipped on any replay
```

## The guarantee (honestly scoped)

Classic idempotency-key pattern adapted for agents: compute a stable key → atomically check-and-claim → if **committed**, return the stored result without re-running; if **in-flight**, block/deny per policy; if new, run and commit. On a crash mid-effect the key is left in-flight and **quarantined** — a half-completed payment must never silently re-fire. Single-writer semantics are strong; multi-writer needs a real transactional store (documented, not overpromised).

Pluggable stores: memory · SQLite · Redis · Postgres. Onchain adapter dedupes by (nonce, calldata-hash). See [`SPEC.md`](./SPEC.md) and [`ROADMAP.md`](./ROADMAP.md).

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
