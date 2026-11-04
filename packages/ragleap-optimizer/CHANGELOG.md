# Changelog

All notable changes to `ragleap-optimizer` are documented here. Format
loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.0] - unreleased

First version (design: #1164). There are no behaviour changes: this is a new package.

### Added

- `Optimizer` with a per-step `Report` (tokens before and after, whether the request changed, whether information was lost). A step that makes a request longer is discarded; the caller's request is never changed.
- Steps: `dedupe_context`, `minify_whitespace`, `stable_prefix` and `trim_tool_output`. Only `trim_tool_output` can lose information, and it reports `lossy=True` when it cuts. (The design issue first listed trimming as lossless; that was wrong and is corrected there.)
- `ExactCache`: in-memory, scope required, whole request in the key, expiry, least-recently-used eviction, erase per scope.
- `retry` and `with_fallback`: bounded attempts, doubling waits with a cap, a deadline, and an error that names the failures without their text.
- `evaluate`: paired comparison on your own cases with a seeded bootstrap interval; no savings figure below 30 cases; regressions listed by case.
- Threat model with 9 rows; `scripts/mutation_check.py` with 84 mutations.

### Verified

- 156 tests pass and all 84 mutations are killed in the author's sandbox (Python 3.12, SQLite not involved). The mutation check was run three times with the same result; one test that caught a mutation only by chance was replaced by a deterministic one.
- A lossless pipeline never made a request longer on 200 random requests.

### Not verified

- Python 3.10 and 3.11, the CI matrix, and a build of the published wheel: not run yet when this was written.
- Any speed or token-saving number on real traffic. No benchmark has been run; the library makes no savings claim.
- Token counts against any real tokenizer.
- Behaviour with message shapes other than the OpenAI-style `messages` list (for example other providers' content blocks beyond text parts).
- Several processes sharing a cache (the cache is per process).
- No independent security review has been done.
