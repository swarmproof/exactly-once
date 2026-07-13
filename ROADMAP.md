# exactly-once — Roadmap

## v0.1
- `@once` decorator + `with once(...)` context manager
- Stores: memory / SQLite / Redis
- Crash-safety + replay-safety
- Docs with payment / email / tx examples

## v0.2
- Onchain adapter (dedupe by nonce + calldata-hash)
- TypeScript port (the JS agent ecosystem is large)
- LangGraph / CrewAI integration helpers

## v0.3
- Reconciliation policy library (crash-during-effect handling)
- Dashboards for quarantined effects
