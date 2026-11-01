# Changelog

## [Unreleased]

## [0.1.0] - not released
### Added
- `Tracer`, `Span`, `traced`, `InMemoryExporter`, `JsonlExporter` (core).
- `Tracer.record()` for already-finished spans; `RagTraceAdapter` for ragleap-rag's on_ingest/on_query/on_answer hooks (allow-listed counts only, event shapes read from ragleap-rag c24a43b).
- Limits: name 128, key 64, 32 attributes, strings cut at 256.
- Content keys (prompt, answer, query, file names, credentials) never recorded.

### Verified
- Core: 34 tests and 24/24 deliberate breaks on the VPS (Python 3.10.12) and in CI on 3.10 and 3.12 (run 41292557648); the workflow goes red when a protection is broken (run 41293119373, 3.12 leg).
- Adapter: 63 tests and 42/42 deliberate breaks in a sandbox (Python 3.12). On the VPS (Python 3.10.12) an earlier version with 41 breaks passed; CI run pending.

### Not verified
- Adapter on the VPS and in CI; any use with a real ragleap-rag instance (the list form of the hooks was read from its source, not run); a real stream stopped early; Windows permissions.

### Limitations
- No retention or rotation; key denylist is not a guarantee; no opt-in content capture.
