# Changelog

## [Unreleased]

## [0.1.0] - 2026-11-06
### Added
- `Tracer`, `Span`, `traced`, `InMemoryExporter`, `JsonlExporter` (core).
- `Tracer.record()` for already-finished spans; `RagTraceAdapter` for ragleap-rag's on_ingest/on_query/on_answer hooks (allow-listed counts only, event shapes read from ragleap-rag c24a43b).
- `integration/` tests and `scripts/integration_break.py` (need ragleap-rag and Postgres; not in the wheel).
- Limits: name 128, key 64, 32 attributes, strings cut at 256.
- Content keys (prompt, answer, query, file names, credentials) never recorded.
- A threat-model section in the README.

### Verified
- Core: 34 tests and 24/24 deliberate breaks on the VPS (Python 3.10.12) and in CI on 3.10 and 3.12 (run 41292557648); the workflow goes red when a protection is broken (run 41293119373, 3.12 leg).
- Adapter: 63 tests and 42/42 deliberate breaks on the VPS (Python 3.10.12) and in CI on 3.10 and 3.12 (run 41294125785).
- Integration against a real RagLeap (ragleap-rag ee8f083, Postgres 16 + pgvector 0.8.0, fake embedder and generator): 9 tests and 12/12 deliberate breaks, in a sandbox and in CI on Python 3.11 (run 41374345874, VERDICT: PASS).
- CI hygiene: the unit workflow's actions are pinned to commit SHAs (PR #1171; its own run 41609315806 shows 63 passed and 42/42 deliberate breaks caught on Python 3.10 and 3.12).
- Release path: a TestPyPI dry run (release workflow run 41977467770, 2026-11-05) published the wheel and the sdist through PyPI trusted publishing; both files carry a provenance record naming ragleap/ragleap-core, release.yml and the testpypi environment; the five modules in the TestPyPI wheel are identical to main; the package installed from TestPyPI into a clean virtual environment and passed a smoke check (version, content key dropped, no dependencies).

### Not verified
- The integration tests on Python versions other than 3.11; real LLM providers (usage keys come from a fake shaped like the real providers); vector backends other than pgvector; spans handed between threads; Windows permissions.

### Limitations
- No retention or rotation; key denylist is not a guarantee; no opt-in content capture.
