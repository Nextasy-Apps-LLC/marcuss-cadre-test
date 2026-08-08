# Files

- [Knowledge base and retrieval](knowledge-base.md) - How cadre grounds answers — the committed LanceDB corpus and manifest, the condense→embed→search retrieve step, citation rendering, the offline ingest pipeline, and the fail-open footguns (manifest/embedding-width mismatch, container-init warm-up).
- [SSE contract v2 — steps, states, tokens](sse-contract.md) - The cadre SSE v2 wire format — the five events (trace, state with elapsed_ms, token, done, error) plus the ping heartbeat; the six pipeline steps in order (retrieve now grounded in the knowledge base); status semantics (degraded, skipped, lost, stream-then-retract); the LangGraph backend and hand-rolled fetch-SSE client; and the tests that pin the contract.
