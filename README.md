# exactly-once

**Idempotency middleware for AI-agent side-effects.** Wrap any tool call that must never fire twice — a payment, an email, an onchain transaction — and it runs **at most once per key**, replaying its stored result across retries, concurrent workers, crashes, and replays.

[![PyPI](https://img.shields.io/pypi/v/exactly-once.svg)](https://pypi.org/project/exactly-once/)
[![Python](https://img.shields.io/pypi/pyversions/exactly-once.svg)](https://pypi.org/project/exactly-once/)
[![CI](https://github.com/swarmproof/exactly-once/actions/workflows/ci.yml/badge.svg)](https://github.com/swarmproof/exactly-once/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](./LICENSE)
[![Typed](https://img.shields.io/badge/typed-py.typed-blue.svg)](https://peps.python.org/pep-0561/)

![An agent crashes mid-payment, resumes, and charges once with @once — versus twice without it](docs/demo.gif)

> This is exactly-once **effect** (at-most-once execution + replay-on-success) — **not** exactly-once *delivery*, which is impossible (Two Generals / FLP). The library is scrupulous about that line; see [Guarantees & limits](#guarantees--limits).

---

## The problem

Agents retry. They crash and resume. They get replayed during debugging. Every one of those can fire a side-effect **twice** — a card charged twice, an email sent twice, a transaction submitted twice. Frameworks give you retries and checkpoints, but *not* idempotency for the effects those retries cause — so you hand-roll dedupe logic, badly, every time.

`exactly-once` is the missing primitive: two lines that make an unsafe retry safe.

## Install

```bash
pip install exactly-once                 # core — zero required dependencies
pip install "exactly-once[redis]"        # + a Redis store
# extras: [redis] · [postgres] · [onchain] · [langgraph] · [crewai]
```

Python 3.11+ · fully typed (`py.typed`) · no LLM, no model costs.

## Quickstart

```python
from exactly_once import once, Store, current_key

store = Store.sqlite("effects.db")       # or .memory() / .redis(url) / .postgres(dsn)

@once(store, key=lambda order, **_: f"charge:{order.id}")
def charge_card(order):
    # pass our key through as Stripe's own idempotency key — belt and suspenders
    return stripe.charge(order.customer, order.amount, idempotency_key=current_key())

charge_card(order)   # runs the charge
charge_card(order)   # replays the stored result — Stripe is NOT called again
```

Inline effects use the context manager. **Async is identical** — `async with` and async callables have the same semantics:

```python
with once(store, key="welcome:user-4471") as guard:
    if guard.fresh:
        guard.result = send_email(...)   # skipped on every replay

async with once(store, key=f"notify:{event_id}") as guard:
    if guard.fresh:
        await post_to_slack(...)
```

> ⚠️ **Key on business identity** (`order_id`), never a mutable value like amount — two distinct \$50 charges must not collapse into one.

**See it stop a double-charge in 15 seconds:**

```bash
python examples/crash_mid_payment.py
```

It crashes an agent mid-payment, resumes, and shows **one** charge with `@once` versus **two** without — side by side. More runnable examples in [`examples/`](./examples/).

## What you get

- **Two-line API** — a `@once` decorator and a `with once(...)` context manager. Sync and async, identical semantics.
- **Pluggable stores** — memory · SQLite · Redis · Postgres — each with a *documented* atomicity and writer model (see the table below).
- **Safe by default on crash** — a crash mid-effect is **quarantined**, never silently re-fired. Opt-in policies (`check_then_decide`, `wait`, `auto_retry`) when you want more.
- **Concurrency-safe** — an ownership/fencing token on every claim; an optional **lease + heartbeat** makes reconciliation safe even across live workers.
- **Onchain adapter** — dedupe transactions by `(chain_id, from, nonce, calldata)`; a resumed agent never double-submits.
- **Framework helpers** — thin `once_node` (LangGraph) and `once_tool_run` (CrewAI) wrappers.
- **Honest by policy** — leads with its limits, and a CI lint fails the build if the docs ever overclaim.
- **Zero required deps, fully typed, zero LLM.** It's plumbing — it works offline, forever.

## Guarantees & limits

The mechanism: compute a stable **key** → atomically **claim** it → if **committed**, replay the stored result without re-running; if **in-flight**, block/deny per policy; if new, run the effect and commit. On a crash mid-effect the key is left in-flight and **quarantined** — a half-completed payment must never silently re-fire.

**It guarantees** (given a store with an atomic claim): the effect is *entered at most once per key* across retries, concurrent workers, crashes, and replays; after a commit, every later call replays the stored result; a concurrent second caller never runs in parallel; a crash mid-effect never auto-re-fires.

**It does not**: promise exactly-once *delivery* (impossible — it's at-most-once execution + replay-on-success, and end-to-end "the world changed once" holds only when composed with an idempotent provider). It cannot know the outcome of a crash mid-effect — it refuses to guess (quarantine), and lets a prober or a provider idempotency key narrow the window. **It is only as strong as the store you pick:**

| Store | Guarantee | Use for |
|---|---|---|
| **memory** | strong within one process | tests, dev |
| **SQLite** | strong on one host | single-node agents, jobs, CI |
| **Redis** | strong single-instance · best-effort under failover | distributed workers sharing one Redis |
| **Postgres** `SERIALIZABLE` | true multi-writer, linearizable | multi-host production |

The full boundary — every guarantee and every limit — is in [`docs/ARCHITECTURE.md` §9](./docs/ARCHITECTURE.md).

## Recipes

**Crash recovery for money movement** — observe the world instead of guessing:

```python
from exactly_once import once, check_then_decide, ProbeResult, Verdict

def prober(key):                      # observe the world: did the charge actually land?
    charge = find_stripe_charge_by_idempotency_key(key)   # your lookup against Stripe
    return ProbeResult(Verdict.COMMITTED, charge) if charge else ProbeResult(Verdict.NOT_COMMITTED)

@once(store, key=lambda o: f"charge:{o.id}", policy=check_then_decide(prober))
def charge_card(order): ...
```

**Concurrent workers** — a lease makes reconciliation safe across live workers (a dead worker's orphan is adopted by exactly one; a live one is never adopted):

```python
@once(store, key=..., policy=check_then_decide(prober), lease_ttl=30.0)
def charge_card(order): ...
```

**Onchain — at-most-once transactions:**

```python
from exactly_once.onchain import onchain_once, TxIntent, Web3ChainClient

chain = Web3ChainClient(w3, private_key)

@onchain_once(store, chain)           # key = (chain_id, from, nonce, calldata)
def payout(to, amount) -> TxIntent:
    return TxIntent(to=to, value=amount)
```

**LangGraph / CrewAI:**

```python
from exactly_once.integrations.langgraph import once_node
from exactly_once.integrations.crewai import once_tool_run

@once_node(store)                     # keys on the run's thread_id + node name
def charge(state, config): ...

class ChargeTool(BaseTool):
    @once_tool_run(store, key=lambda self, order_id, **_: f"charge:{order_id}")
    def _run(self, order_id): ...
```

## How it compares

`exactly-once` is a **library that guards the effect boundary** — not a replacement for a workflow engine. It composes with all of these.

| | **exactly-once** | Temporal / Restate / DBOS | AWS Lambda Powertools | Stripe idempotency keys |
|---|---|---|---|---|
| **What** | a two-line library | a durable-execution runtime you adopt | a Lambda-only utility | a single provider's feature |
| **Scope** | the effect boundary, anywhere | orchestration *and* the effect boundary | Lambda handlers | one API |
| **Crash mid-effect** | quarantine — never auto-re-fire | activity re-runs; *you* make it idempotent | **deletes** the record and re-runs | replays the cached response |
| **Adoption cost** | `pip install`, two lines | adopt a runtime | be on AWS Lambda | be on Stripe |

Every durable-execution engine, at the effect boundary, reduces to "at-least-once + an idempotency key." `exactly-once` is that reduction as a drop-in — with a *safe* default for the crash it can't otherwise resolve.

## Documentation

- **[SPEC.md](./SPEC.md)** — the design spec & PRD.
- **[docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md)** — the state machine, the store contract, and §9 Guarantees & Limits (read this before trusting anything).
- **[CHANGELOG.md](./CHANGELOG.md)** · **[examples/](./examples/)** · **[ROADMAP.md](./ROADMAP.md)**

## Development

```bash
uv venv --python 3.11 && uv pip install -e ".[dev]"
uv run pytest                                # full suite (Redis/Postgres need Docker; onchain needs Foundry)
uv run mypy src/exactly_once                 # strict typing
uv run ruff check src tests examples scripts # lint
uv run python scripts/check_docs_honesty.py  # the docs-honesty gate
uv run python scripts/benchmark.py           # per-call overhead
```

Overhead per guarded call is one store round-trip plus key/codec work — a few microseconds on the in-memory store; real deployments are dominated by the store's own latency.

## Part of the Swarm Proof toolkit

*Trust infrastructure for the agent economy — seven projects, one thesis.*

| Project | What it does |
|---------|--------------|
| [stampede](https://github.com/swarmproof/stampede) | Point a herd of realistic agents at your system before real ones arrive |
| [mockworld](https://github.com/swarmproof/mockworld) | A synthetic internet for agents — fake Stripe, Gmail, exchange, instantly |
| [mcp-probe](https://github.com/swarmproof/mcp-probe) | The CI quality suite for MCP servers — lint, contract-test, benchmark, load |
| [costbomb](https://github.com/swarmproof/costbomb) | Denial-of-wallet fuzzing — find the inputs that make your agent spend \$500 |
| **exactly-once** ← *you are here* | Idempotency middleware so agent side-effects fire once |
| [agent-postmortems](https://github.com/swarmproof/agent-postmortems) | A structured incident database + post-mortem standard for agent failures |
| [awesome-agent-reliability](https://github.com/swarmproof/awesome-agent-reliability) | The curated map of the field |

## License

[MIT](./LICENSE). Citable via [`CITATION.cff`](./CITATION.cff).
