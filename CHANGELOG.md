# Changelog

All notable changes to **exactly-once** are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] — the primitive

First public release.

### Added
- `@once` decorator and `with once(...) as guard:` context manager — sync **and** async.
- Store adapters, each with a documented atomicity and writer model:
  - `memory` — in-process (tests/dev).
  - `sqlite` — single-host, WAL-durable.
  - `redis` — distributed against one instance (Lua compare-and-set).
  - `postgres` — true multi-writer (`SERIALIZABLE` + `ON CONFLICT DO NOTHING RETURNING`).
- Crash-mid-effect default policy: **quarantine** (never a silent re-fire). Opt-in
  `fail`, `wait`, `auto_retry`, and `check_then_decide` (world-observing prober) policies.
- Ownership/fencing token on every claim so `release` is a compare-and-delete — a
  reconciler cannot retire a newer claim of the same key.
- Provider-key passthrough via `current_key()`; payload fingerprinting (`KeyReuseError`);
  key namespacing; an inspectable ledger (`get` / `list(state=...)`).
- Result codec (JSON default) with a 1 MiB size ceiling (`ResultTooLargeError`).
- Fail-closed-by-default when the store is unreachable (`on_store_down="open"` to opt out).
- `py.typed` (PEP 561); zero required heavy dependencies; optional `[redis]` / `[postgres]` extras.

### Guarantees
Exactly-once **effect** (at-most-once execution + replay-on-success), not exactly-once
**delivery**. The full boundary — including the store-by-store guarantee table and the
single-reconciler scope of `check_then_decide` / `auto_retry` — is in
[`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md) §9.

### Not yet (planned for v0.2)
Onchain adapter (dedupe by nonce + calldata-hash), TypeScript port, LangGraph/CrewAI
helpers, lease/heartbeat-based reconciliation for the concurrent-reconciler case.

[Unreleased]: https://github.com/swarmproof/exactly-once/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/swarmproof/exactly-once/releases/tag/v0.1.0
