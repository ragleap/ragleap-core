# Changelog

## [Unreleased]

## [0.1.0] - not released
### Added
- `Tracer`, `Span`, `traced`, `InMemoryExporter`, `JsonlExporter` (core only).
- Limits: name 128, key 64, 32 attributes, strings cut at 256.
- Content keys (prompt, answer, query, file names, credentials) never recorded.

### Verified (sandbox, Python 3.12)
- 34 tests pass; `scripts/mutation_check.py`: 24/24 deliberate breaks caught.

### Not verified
- Python 3.10; CI run; wheel installed into a clean environment; any use with ragleap-rag; streaming, threads, Windows permissions.

### Limitations
- No retention or rotation; key denylist is not a guarantee; no opt-in content capture.
