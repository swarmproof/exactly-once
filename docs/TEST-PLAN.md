# exactly-once — TEST PLAN

*Correctness is the product. Testing is first-class, not a phase. This is the highest correctness bar in the Swarm Proof portfolio.*

> The thesis of the whole library is a single invariant — **the wrapped effect is entered at most once per key** — so the test strategy is organized around *trying as hard as possible to break that invariant*: concurrently, across crashes, across replays, and across every store. A green suite is the guarantee.

---

## 1. Strategy & the test pyramid

```
                 ▲  fewer, slower, highest-value
                 │   ┌──────────────────────────────────────────┐
     e2e (§2)    │   │ agent crashes mid-payment, resumes, does   │
                 │   │ NOT double-charge (with vs without)        │
                 │   └──────────────────────────────────────────┘
   integration   │   ┌──────────────────────────────────────────┐
   (§3, §4)      │   │ concurrency races + crash-injection PER     │
                 │   │ store (memory/SQLite/Redis/Postgres)        │
                 │   └──────────────────────────────────────────┘
   property      │   ┌──────────────────────────────────────────┐
   (§5)          │   │ Hypothesis state-machine model: no reachable│
                 │   │ operation sequence yields >1 execution      │
                 │   └──────────────────────────────────────────┘
   unit          │   ┌──────────────────────────────────────────┐
                 │   │ key derivation/normalization, codec,        │
                 │   │ fingerprint, policies, error taxonomy       │
                 ▼   └──────────────────────────────────────────┘
                     more, faster, foundational
```

**Two testing philosophies drive the middle two layers:**
- **Adversarial, not illustrative.** We do not test that the happy path works; we test that every *un*happy path cannot double-fire. Every crash point is enumerated; every interleaving is fuzzed.
- **A test-double effect with a hard counter.** All correctness tests wrap a `SpyEffect` that increments a *durable* counter (in the same store/file so it survives an injected crash). The core assertion, everywhere, is `spy.execution_count == 1`.

---

## 2. End-to-end scenarios (Given/When/Then)

### E2E-1 — The flagship: crash mid-payment, resume, do not double-charge
```
GIVEN an agent that charges a card via a fake Stripe (mockworld) with idempotency-key support
  AND the charge is wrapped: @once(store=SQLite, key="charge:{order_id}", policy=quarantine)
  AND the exactly-once key is passed through as Stripe's Idempotency-Key (ADR-005)
WHEN the process is killed AFTER stripe.charge returns 200 but BEFORE commit() lands
  AND the agent process is restarted and the run resumes
THEN the fake Stripe shows exactly ONE charge for that order
  AND the resumed run observes the key as IN_FLIGHT and QUARANTINES it (no second charge)
  AND after a check_then_decide prober queries fake-Stripe by idempotency key, the key is
      marked COMMITTED and the stored result is replayed
```

### E2E-2 — Control: the SAME scenario WITHOUT exactly-once (proves the problem)
```
GIVEN the identical agent with NO @once wrapper (naive retry-on-resume)
WHEN killed after the charge, before the local "done" flag is written, then resumed
THEN the fake Stripe shows TWO charges  ← this is the bug the library exists to prevent
```
E2E-1 and E2E-2 run in the same test module and render the side-by-side demo GIF (DELIVERY-PLAN §8).

### E2E-3 — Replay safety (debug re-run)
```
GIVEN a committed key from a prior successful run (durable store)
WHEN the entire agent script is re-executed from scratch (replay/debug)
THEN send_email fires ZERO additional times and the stored result is returned
```

### E2E-4 — Onchain double-submit prevention (v0.2)
```
GIVEN onchain_once over a signer against an Anvil/Hardhat mainnet fork
  AND key = (chain_id, from, nonce, calldata_hash)
WHEN the agent broadcasts a tx, then crashes before commit, then resumes
THEN the resume prober finds the tx in mempool/mined and replays the tx hash
  AND NO second transaction is signed at a new nonce
  AND when the first tx was actually dropped, the resume re-signs at the SAME nonce (not nonce+1)
```

### E2E-5 — Framework integration smoke (v0.2)
```
GIVEN a LangGraph node and a CrewAI tool each wrapping an effect with @once
WHEN the graph/crew is re-invoked with the same inputs
THEN the effect runs once; semantics match the raw-loop case
```

---

## 3. Concurrency race tests (two workers, one key)

The single most important integration suite. Run **per store**.

| ID | Scenario | Assertion |
|---|---|---|
| RACE-1 | N=2..64 threads/tasks call `once(key=K)` simultaneously (barrier-synchronized on the claim) | `spy.execution_count == 1`; exactly one caller sees `FRESH`; all others see `IN_FLIGHT`/`COMMITTED` |
| RACE-2 | N workers, sync **and** async variants (memory `asyncio.Lock`; SQLite; Redis; Postgres) | same; run ≥10,000 trials per store (DoD gate) |
| RACE-3 | Loser policy: block-until-committed vs. deny-immediately | blocked losers eventually get the committed result; denied losers raise, never execute |
| RACE-4 | Interleave `claim` and `commit` from different workers on the same key | no worker executes after another has committed; commit is idempotent |
| RACE-5 | Multi-process (not just multi-thread) on SQLite (same file) and Redis/Postgres (shared) | `spy.execution_count == 1` across OS processes |
| RACE-6 | Redis single-instance vs. documented failover caveat | single-instance: strong (count==1); a failover-injection test *documents* (asserts on) the best-effort boundary rather than pretending it's linearizable |

**Tooling:** `pytest` + threads/`asyncio` + `multiprocessing`; a `Barrier` to maximize contention at the claim; `hypothesis` to fuzz N and timing jitter. Postgres/Redis via testcontainers.

---

## 4. Crash-injection tests (per store)

Crash points are enumerated relative to the effect boundary. A `crashpoint(name)` hook raises `HardCrash` (or `os._exit` in the process variant) at each labeled site; the test then reopens the store and resumes.

| ID | Crash point | Expected post-resume state | Assertion |
|---|---|---|---|
| CRASH-1 | after `claim` (FRESH), **before** effect | IN_FLIGHT | resume: quarantine by default; `check_then_decide` prober returns `not_committed` → `release` + re-run → count==1 |
| CRASH-2 | **during** effect (effect not done) | IN_FLIGHT | resume: quarantine; prober `not_committed` → re-run → count==1 |
| CRASH-3 | after effect returns, **before** `commit` (the killer window) | IN_FLIGHT | **default: quarantine, count stays 1, NO auto-refire** (this is the core safety property); prober `committed` → mark COMMITTED, replay, count==1 |
| CRASH-4 | after `commit`, before returning to caller | COMMITTED | resume: replay stored result, count==1 |
| CRASH-5 | mid-`commit` write (partial durable write) | store-dependent | durable stores: either the commit is atomic (COMMITTED) or absent (IN_FLIGHT→quarantine); never a corrupt half-record |

**Process-kill variant (strongest):** for SQLite, a subprocess is `SIGKILL`ed at each crash point, then a fresh process reopens the file — proving true crash-safety, not just exception-safety. The `SpyEffect` counter is persisted in the same SQLite file so it survives the kill. Repeat for Redis/Postgres with a shared backing service.

**Anti-Powertools regression (⊕):** an explicit test asserts that an effect that raises an exception (not a crash) leaves the key `IN_FLIGHT`/quarantined and does **NOT** delete/release it — guarding against ever regressing to the delete-on-exception behavior (ADR-004).

---

## 5. Property-based testing (the state machine)

The crown jewel. Model exactly-once as a `hypothesis` stateful/RuleBasedStateMachine and let it search for a counterexample to the invariant.

**Model:**
- Commands: `claim(key)`, `run_effect(key)` (only legal if last claim returned FRESH), `commit(key, result)`, `crash_and_resume()`, `release(key)` (only legal pre-effect), `replay(key)`.
- Shrinkable across multiple keys, multiple simulated workers, and arbitrary crash injection between any two commands.

**Invariants checked after every command (for every key):**
| ID | Invariant |
|---|---|
| INV-1 | `effect_executions[key] <= 1` — **the** invariant (at-most-once execution) |
| INV-2 | A key observed COMMITTED never transitions back to FRESH/IN_FLIGHT except via explicit `release`/force-refire commands |
| INV-3 | At most one worker ever observes FRESH for a key |
| INV-4 | `commit` is idempotent — replaying it doesn't change stored result or count |
| INV-5 | A crash never advances a key to COMMITTED without a real commit having occurred |
| INV-6 | Default policy never issues a `run_effect` for an orphaned IN_FLIGHT (no auto-refire) |
| INV-7 | If provider-passthrough is modeled, end-to-end `world_mutations[key] <= 1` even under force-refire |

**Metamorphic property:** for any command sequence `S`, the observable result of the guarded call equals the result of running the raw effect exactly once — i.e., exactly-once is *transparent on the happy path* and *strictly safer on every unhappy path*.

Run against the **memory store model** (fast, exhaustive) in CI on every PR; run a reduced sweep against SQLite/Redis nightly.

---

## 6. Unit tests

| Area | Cases |
|---|---|
| Key derivation (REQ-K1–K3) | static key; callable key; derived-from-args; kwargs order-insensitivity; **`UnstableKeyError`** on unhashable/unstable inputs (e.g., a lambda that reads `datetime.now()`) |
| Anti-pattern guard (REQ-K6) | a doc-test that the recommended `key=charge:{order_id}` dedupes two same-amount charges as distinct, and the value-in-key form does not (demonstrates the footgun) |
| Fingerprint (REQ-K4) | same key + different fingerprint → `KeyReuseError`; same key + same fingerprint → replay |
| Codec (REQ-S6) | JSON round-trip; non-serializable result → clear error; "store a reference" pattern; size ceiling |
| Policies (REQ-R1–R3) | quarantine returns `QuarantinedError`/sentinel; fail raises; `auto_retry` re-runs (email case); `check_then_decide` honors prober verdicts (committed/not_committed/unknown) |
| Store-down (REQ-S7, ADR-006) | fail-closed default raises without running effect; fail-open opt-in runs it |
| Ledger (REQ-S9) | `list(state=IN_FLIGHT)` enumerates quarantined keys; `get` returns record; used exactly as stampede will |
| Async parity (NFR-3) | every semantic above has a mirrored `async with` / async-callable test |
| Error taxonomy | every public exception type is raised by exactly one documented condition |

---

## 7. CI gates (merge-blocking)

| Gate | Definition | Blocks merge? |
|---|---|---|
| G-PROP | Property state-machine suite (§5) green; INV-1 never violated across the search budget | ✅ |
| G-RACE | Concurrency races (§3) green; ≥10k trials/store show count==1 | ✅ |
| G-CRASH | Crash-injection (§4) green incl. the CRASH-3 no-auto-refire and anti-Powertools regression | ✅ |
| G-E2E | E2E-1/-2/-3 green (the flagship + control + replay) | ✅ |
| G-TYPE | `mypy --strict` + ruff clean; `py.typed` present | ✅ |
| G-DOCS ⊕ | docs-honesty lint: no occurrence of "exactly-once" in `docs/`/README without a scoping qualifier ("effect", "at most once", "not delivery") within N tokens | ✅ |
| G-COV | branch coverage on the core state machine + adapters ≥ 95% (correctness code, not a vanity number) | ✅ |

Matrix: Python 3.11/3.12/3.13 × {memory, SQLite always; Redis, Postgres via testcontainers}. v0.2 adds a TS suite mirroring §2–§6 and an onchain job against an Anvil fork.

---

## 8. Acceptance criteria (v0.1 ships when)

1. **INV-1 has never been observed to fail** across the full property search budget, on any store.
2. **E2E-1 passes and E2E-2 fails-as-expected** (the control proves the tool does something), rendered as the side-by-side GIF.
3. **CRASH-3 (post-effect, pre-commit) results in quarantine, never a second execution**, on SQLite and Redis, including the `SIGKILL` process-kill variant.
4. **RACE (two workers, one key) shows exactly one execution** across ≥10k trials on every shipped store.
5. **No supported configuration can be made to double-fire** by the adversarial suite — this is the DoD line the trust brand rests on.
6. All CI gates (§7) green on the full matrix.
7. The "Guarantees & Limits" section is verified by a reviewer to match actual test coverage — every G-* guarantee has a test, every L-* limit has a test proving the library *doesn't* claim more.

---

## 9. Test infrastructure & dependencies

- **Frameworks:** `pytest`, `hypothesis` (property + stateful), `pytest-asyncio`, `testcontainers` (Redis/Postgres), `multiprocessing`/subprocess for process-kill crashes.
- **Fakes:** `mockworld`'s fake Stripe (portfolio synergy) for E2E-1/-2; a local `SpyEffect` with a store-persisted counter for correctness assertions; Anvil/Hardhat fork for onchain (v0.2).
- **Crash hook:** a `crashpoint(name)` injection layer, no-op in prod builds, that raises/`_exit`s at labeled sites; enumerated so every boundary in ARCH §1.4 has a corresponding CRASH-* test.
- **Portfolio reuse:** the ledger assertions in §6 are the *same* API `stampede` calls in its chaos assertions — so exactly-once's own test suite doubles as the contract test for the stampede integration (DELIVERY-PLAN §7).
