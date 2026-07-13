# exactly-once — DELIVERY PLAN

*Milestones, WBS, sequencing, sizing, definition-of-done, and how the portfolio dogfoods it into existence.*

> Extends `ROADMAP.md`. Sizing uses evening-units (the portfolio's honest capacity model from `PORTFOLIO-ROADMAP.md`: this project is an **S**, targeted Weeks 4–8, built in parallel with the mcp-probe wedge). The correctness bar is the highest in the portfolio, so the plan front-loads the test harness (see `TEST-PLAN.md`).

---

## 1. Milestones

| Milestone | Theme | Target | Ships |
|---|---|---|---|
| **v0.1 — the primitive** | Correct, honest, tiny | Wk 4–8 | `@once` + `with once`, memory/SQLite/Redis stores, crash+replay safety, quarantine default, inspectable ledger, sync+async, "Guarantees & Limits" docs, payment/email/tx examples |
| **v0.2 — reach** | Meet users where they are | post-launch | Onchain adapter, TypeScript port, LangGraph/CrewAI helpers, Postgres adapter (if not in v0.1), `auto_retry`/`check_then_decide` policies |
| **v0.3 — operate** | Run it in production | later | Reconciliation policy library, quarantine dashboard, framework-edge middleware, OTel/metrics exporter |

Guiding principle: **v0.1 ships correctness, not features.** A double-fire in a supported config is a project-ending bug for a trust-brand library; a missing convenience helper is not.

---

## 2. Work breakdown structure (v0.1)

| WBS | Work item | Reqs (PRD) | Size | Depends on |
|---|---|---|---|---|
| **W1** | Core state machine (`FRESH/IN_FLIGHT/COMMITTED`), `ClaimResult`, key resolution pipeline | REQ-A1–A5, REQ-K1–K3 | M | — |
| **W2** | Store `Protocol` + `ClaimResult`/`State` types + result codec (JSON) | REQ-S1, REQ-S6 | S | W1 |
| **W3** | **memory** adapter (+ `asyncio.Lock`) | REQ-S2–S4 | S | W2 |
| **W4** | **SQLite** adapter (`INSERT OR ABORT` / `BEGIN IMMEDIATE`) | REQ-S2–S4 | M | W2 |
| **W5** | **Redis** adapter (`SET NX` + Lua CAS) | REQ-S2–S4 | M | W2 |
| **W6** | Decorator `@once` + sync/async context manager, commit-on-clean-exit / no-commit-on-exception | REQ-A1–A4, REQ-C3, NFR-3 | M | W1–W3 |
| **W7** | Quarantine + fail reconciliation policies; `QuarantinedError` | REQ-R1–R2, REQ-C3–C4 | S | W6 |
| **W8** | Inspectable ledger (`get`/`list` by state) — the stampede/costbomb hook | REQ-S9, REQ-R4 | S | W2 |
| **W9** | Payload fingerprinting + `KeyReuseError`; key namespacing | REQ-K4–K5 | S | W1 |
| **W10** | Store-unavailable fail-closed policy | REQ-S7, ADR-006 | S | W6 |
| **W11** | **Test harness** — property-based state-machine tests, crash-injection per store, concurrency race tests (see TEST-PLAN) | all | **L** | W3–W7 |
| **W12** | Docs: README "Guarantees & Limits", payment (with provider-key passthrough), email, tx examples; anti-pattern callout | NFR-5, REQ-K6, ADR-005 | M | W6–W8 |
| **W13** | Packaging: `py.typed`, optional extras (`[redis]`, `[postgres]`, `[onchain]`), CI, PyPI release | NFR-4, NFR-9 | S | all |
| **W14** | Postgres adapter (`ON CONFLICT` + `SERIALIZABLE`) — the true-multi-writer answer | REQ-S4 | M | W2 (stretch for v0.1) |

**Critical path:** W1 → W2 → (W3/W4/W5) → W6 → W7 → **W11** → W12 → W13. W11 is the long pole and the whole point.

---

## 3. Sequencing rationale

1. **State machine + memory store + context manager first (W1–W3, W6).** This gets a runnable, testable primitive in one evening-cluster and lets W11's property tests start immediately against the in-memory store (fastest feedback loop for correctness).
2. **Durable stores next (W4, W5).** SQLite before Redis — SQLite proves crash-safety on a single host with no external dependency, which is the cleanest crash-injection target (kill the process, reopen the file).
3. **Policies + ledger (W7, W8) before docs.** The quarantine story and the ledger are the differentiators; docs can't be written honestly until they exist.
4. **Test harness runs continuously (W11), not at the end.** For this project, tests are not a phase — they gate every merge (see §6).
5. **Postgres (W14) is a v0.1 stretch.** Redis covers the common distributed case; Postgres is the "true multi-writer" upgrade and can slip to v0.2 without weakening the v0.1 story, as long as the multi-writer boundary is documented (it is — ARCH §7).

---

## 4. Effort sizing (evening-units, honest)

| Milestone | Estimate | Notes |
|---|---|---|
| v0.1 | ~10–14 evenings | W11 (tests) is ~⅓ of that; correctness bar justifies it |
| v0.2 | ~8–10 evenings | TS port is the bulk; onchain adapter needs a testnet/fork harness |
| v0.3 | ~6–8 evenings | mostly built on stampede's shared report-renderer |

This matches the portfolio's **S** classification and the Wk 4–8 window, parallelizable with mcp-probe.

---

## 5. Definition of done

### v0.1 DoD
- [ ] All `REQ-*` marked v0.1-must implemented; all `NFR-*` satisfied.
- [ ] Property-based state-machine tests pass (no reachable state sequence produces a double-fire) — TEST-PLAN §5.
- [ ] Crash-injection suite passes for memory (n/a-durability documented), SQLite, and Redis — TEST-PLAN §4.
- [ ] Concurrency race test (two workers, one key) shows exactly one effect execution across ≥10k trials per store — TEST-PLAN §3.
- [ ] The flagship e2e scenario passes: *agent crashes mid-payment, resumes, does NOT double-charge* — with and without exactly-once, side by side — TEST-PLAN §2.
- [ ] README leads with "Guarantees & Limits"; no doc says "exactly-once" without the effect/delivery scoping nearby (NFR-5).
- [ ] The value-in-key anti-pattern is corrected everywhere and explained (REQ-K6).
- [ ] `pip install exactly-once` works; extras resolve; `py.typed` ships; CI green on 3.11/3.12/3.13.
- [ ] Ledger API stable and documented (portfolio contract, PRD C3).

### v0.2 / v0.3 DoD
- v0.2: onchain double-submit-prevention e2e passes on a mainnet fork; TS port passes a mirrored test suite; framework helpers have a smoke test each.
- v0.3: quarantine dashboard renders a live ledger; policy library has property tests per policy.

---

## 6. CI gates (correctness is not optional)

Every PR must pass, or it does not merge:
1. **Property-based state-machine tests** (Hypothesis) — the double-fire invariant.
2. **Concurrency race tests** — N workers, one key, exactly one execution.
3. **Crash-injection tests** — per durable store.
4. **Type check** (mypy strict) + lint.
5. **Docs honesty check** ⊕ — a CI lint that fails if "exactly-once" appears in docs without a nearby scoping qualifier (cheap, keeps the brand honest as contributors arrive).

See `TEST-PLAN.md §7` for the gate definitions.

---

## 7. How the portfolio dogfoods it (seeding strategy)

Because exactly-once is a **dependency**, adoption depth (PyPI dependents) matters more than stars — so the portfolio is engineered to be the first and reference adopter:

| Adopter | How it uses exactly-once | What it proves |
|---|---|---|
| **stampede** | Its chaos injector kills agents mid-effect, then **asserts against exactly-once's ledger**: "was every side-effect exactly-once after the crash?" The ledger `list(state=...)` API (REQ-S9) is the assertion surface. | exactly-once is correct under real chaos, not just unit tests. This is the flagship demo. |
| **costbomb** | In loop/retry-storm fuzzing, wraps the cost-incurring effect in `@once` to prove that a denial-of-wallet retry loop still fires the *effect* once even while burning tokens. | separates "expensive" from "duplicated" — sharpens both tools' stories. |
| **Cairn** | Positioned as the **effect-layer companion** to Cairn's state-recovery layer: Cairn restores agent *state*, exactly-once guarantees the *effects* during that recovery don't double-fire. Case study #1. | the "state + effect" pairing that neither tool has alone. |

Sequencing: land exactly-once v0.1 in the Wk 4–8 window so stampede's flagship build (Wk 6–16) can consume it from the start — the two bootstrap each other, exactly as the portfolio synergy map intends.

---

## 8. Launch checklist

- [ ] **Essay: "Exactly-Once: The Primitive Agent Frameworks Forgot."** Thesis: durable-execution engines all reduce, at the effect boundary, to "at-least-once + idempotency keys" — and here's that reduction as a two-line library that defaults to *safe*. Position explicitly vs. Temporal/Restate/DBOS (RESEARCH §2.2) and vs. the "exactly-once is impossible" crowd (own the honesty).
- [ ] **Demo GIF: crash-mid-payment, side by side.** Left pane: naive agent crashes mid-charge, resumes, **double-charges** (two Stripe events). Right pane: same agent wrapped in `@once`, resumes, **charges once** and quarantines nothing / replays. This is the whole pitch in 15 seconds — the single most important launch asset.
- [ ] README: <90s GIF above the fold; ≤10-line quickstart; "Guarantees & Limits" prominent; sibling links (Swarm Proof toolkit).
- [ ] PyPI published with extras; `CITATION.cff` current.
- [ ] 3–5 seeded `good-first-issue`s (e.g., "add a MongoDB adapter", "add a Slack reconciliation notifier").
- [ ] stampede case study drafted (crash-injection asserting the ledger) to ship *with* stampede's launch.
- [ ] Cross-link from `awesome-agent-reliability` and the Cairn/Trust-Layer essays.
- [ ] Show HN / relevant communities, led by the crash-mid-payment GIF, not the API.

---

## 9. Risks to delivery

| Risk | Likelihood | Mitigation |
|---|---|---|
| Redis-under-failover correctness gets over-claimed by a contributor | med | CI docs-honesty lint (§6.5) + explicit adapter table (ARCH §3.1); Redis marked best-effort under failover in code docstrings |
| Async context-manager semantics subtly wrong across `await` | med | dedicated async race tests in W11; single event-loop assumption documented |
| Scope creep toward "workflow features" | med | NG1 in PRD is load-bearing; every feature request tested against "does a library need this?" |
| TS port drifts from Python semantics | low (v0.2) | shared store-contract spec + mirrored test suite |
| The crash-window is misunderstood as "solved" | med | "Guarantees & Limits" leads the docs; the GIF shows quarantine, not magic |
