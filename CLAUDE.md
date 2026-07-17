# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is (read first)

**exactly-once** is idempotency middleware for agent side-effects: wrap any tool call that must never fire twice (a payment, an email, an onchain tx) and it runs a single time across retries, crashes, and replays.

**There is no source code yet — this is a spec-first repository.** The product right now *is* the design corpus under `docs/` plus `SPEC.md`. Before writing any implementation, treat these documents as the source of truth, not background reading:

- `SPEC.md` — the v1.0 design spec + PRD sketch (state machine, API, store contract, ADR summaries).
- `docs/PRD.md` — the authoritative requirements list. Every requirement has a **stable ID** (`REQ-K*`, `REQ-S*`, `REQ-A*`, `REQ-C*`, `REQ-R*`, `REQ-O*`, `NFR-*`) referenced from the other docs.
- `docs/ARCHITECTURE.md` — the mechanism, full state diagram, store-adapter atomicity table (§3.1), and the **ADRs** (`ADR-001`..`ADR-007`). §9 "Guarantees & Limits" (`G-*` / `L-*`) is explicitly the load-bearing section.
- `docs/TEST-PLAN.md` — the correctness contract. Invariants (`INV-1`..`INV-7`), crash points (`CRASH-1`..`CRASH-5`), race scenarios (`RACE-*`), CI gates (`G-*`).
- `docs/DELIVERY-PLAN.md` — WBS (`W1`..`W14`), critical path, milestones, definition-of-done.
- `docs/RESEARCH.md` — prior-art positioning (vs. Temporal/Restate/DBOS, AWS Powertools, Stripe idempotency keys).

These IDs are a real traceability graph: a change to behavior should be traceable from a `REQ-*` through an `ADR-*` to an `INV-*` and its `CRASH-*`/`RACE-*` test. When implementing, cite the IDs you are satisfying in commits/PRs.

## The one invariant everything serves

> **INV-1 / G-1: a guarded effect is *entered at most once per key*** — across retries, concurrent workers, crashes, and replays.

Crash-safety, replay-safety, and concurrency-safety are all *consequences* of one thing being correct: the store's `claim(key)` is an **atomic check-and-set** with exactly one `FRESH` winner. The library itself is stateless between calls (`ADR-002`); correctness is delegated to that single primitive per store. Keep this framing when reasoning about any change.

State machine (only three states, deliberately — `ADR-007`):
`FRESH` (no record) → `claim` CAS → `IN_FLIGHT` (claimed, no result) → `commit` → `COMMITTED` (result stored, replayed thereafter).

## Non-negotiable design rules (violating these is a project-ending bug, not a style nit)

- **Quarantine is the default crash-mid-effect policy (`ADR-003`, `REQ-R2`, `G-4`).** On resume, an orphaned `IN_FLIGHT` key is *never* auto-re-fired. A half-completed payment of unknown outcome must be surfaced for an explicit human/prober decision, never guessed.
- **`release` is pre-effect only (`ADR-004`, `REQ-S5`).** `IN_FLIGHT → FRESH` is legal only before the effect boundary, or after an explicit `not_committed` verdict. Never auto-release on a post-effect exception — that is the AWS-Powertools delete-on-exception behavior this library exists to reject. An effect that raises leaves the key `IN_FLIGHT`/quarantined. There is a dedicated "anti-Powertools regression" test guarding this.
- **Key on business identity, never a mutable value (`REQ-K6`).** `key=f"charge:{order_id}"`, not `key=f"charge:{customer}:{amount}"`. The mutable-value form collapses two distinct charges into one and forks on a repriced retry. Docstrings/docs/examples must use the correct form and explain why.
- **Fail-closed when the store is unreachable (`ADR-006`, `REQ-S7`).** Default: do not run the effect. Fail-open is an explicit opt-in.
- **Provider-key passthrough is mandatory for money movement (`ADR-005`, `REQ-A5`).** `guard.key` must be retrievable *before* the effect runs so it can be passed as the provider's own idempotency key (Stripe `Idempotency-Key`, onchain nonce). This is belt-and-suspenders: our store gives at-most-once at the call site; the provider gives "world changed once" end-to-end.
- **Every store adapter must document its atomicity + writer model (`REQ-S3`, ARCH §3.1).** An adapter that cannot make `claim` atomic is not a valid adapter (`ADR-002`). The guarantee you get is exactly the guarantee of the adapter row: memory = single-process, SQLite = single-host, Redis = single-instance-strong / failover-best-effort, Postgres `SERIALIZABLE` = true multi-writer. Never over-claim (especially Redis-under-failover).
- **Honesty is linted (`NFR-5`, gate `G-DOCS`).** Do not write "exactly-once" in docs/README without an effect/at-most-once/not-delivery scoping qualifier nearby. It is "exactly-once *effect*" (at-most-once execution + replay-on-success), never "exactly-once *delivery*" (which is impossible — Two Generals / FLP; see `L-1`).

## Stack, layout & commands (v0.1 implemented)

- **Language:** Python 3.11+ (uses `StrEnum`); TypeScript port is v0.2. Core has zero required heavy deps; backends are optional extras (`exactly-once[redis]`, `[postgres]`, `[onchain]`). Ships `py.typed` (`NFR-4`, `NFR-9`). Build backend: hatchling; env managed with `uv`.
- **Layout:** `src/exactly_once/` — `core.py` (the `once` state machine), `stores/` (`base.py` ABC + memory/sqlite/redis/postgres adapters), `keys.py`, `codec.py`, `policies.py`, `errors.py`, `_types.py`. Tests in `tests/`, runnable examples in `examples/`, the docs-honesty gate in `scripts/check_docs_honesty.py`.
- **Commands** (prefix with `uv run`, or activate `.venv`):
  - `uv venv --python 3.11 && uv pip install -e ".[dev]"` — set up.
  - `pytest` — full suite; `pytest tests/test_property.py` etc. for one file; `pytest -k name` for one test. Redis/Postgres tests auto-skip without Docker.
  - `mypy src/exactly_once` (strict) · `ruff check src tests examples` · `python scripts/check_docs_honesty.py`.
  - `pytest --cov=exactly_once --cov-report=term-missing` — coverage.
- **Tests:** `pytest`, `hypothesis` (`RuleBasedStateMachine` in `test_property.py` — the crown jewel, TEST-PLAN §5), `pytest-asyncio` (auto mode), `testcontainers` (Redis/Postgres, `test_backends.py`), `multiprocessing` + `os._exit` for true `SIGKILL` crash-injection (`_mp_helpers.py`, `test_crash.py`).
- **Quality gates (merge-blocking, DELIVERY-PLAN §6 / TEST-PLAN §7), wired in `.github/workflows/ci.yml`:** property state-machine suite, concurrency races (one execution/store), crash-injection incl. the SIGKILL variant, `mypy --strict`, `ruff`, the docs-honesty lint, and ≥95% branch coverage on the core state machine + memory/sqlite adapters. Tests are a continuous gate, not a final phase.

## The flagship deliverable

The single most important asset (DELIVERY-PLAN §8, TEST-PLAN E2E-1/E2E-2) is the side-by-side demo: an agent killed *after* `stripe.charge` returns 200 but *before* `commit` lands, then resumed — **with** `@once` it charges once and quarantines; **without** it double-charges. Correctness work should keep this scenario runnable and green.

## Portfolio contract (why the ledger API is load-bearing)

exactly-once is part of the Swarm Proof toolkit and is a *dependency*, so PyPI dependents matter more than stars. The inspectable ledger (`store.list(state=...)`, `REQ-S9`) is a **stable public contract**: sibling projects `stampede` (chaos-injects crashes, then asserts "was every effect exactly-once?") and `costbomb` assert against it. Breaking the store/ledger interface is a breaking change for the whole portfolio (`C3`). Cairn is positioned as the state-recovery companion (it restores state; exactly-once guarantees effects don't double-fire during recovery).

## Conventions

- **Commits:** Conventional Commits (`feat:`/`fix:`/`docs:`/`refactor:`/`test:`/`chore:`), atomic, imperative, no AI attribution. History so far is `docs:`-only.
- **Docs are still the deliverable pre-code.** A behavior change lands in the relevant `docs/` file (with its IDs) as part of the same change, not afterward.
