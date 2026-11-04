# ragleap-optimizer 0.1.0: threat model

Status: first version. It covers only what 0.1.0 contains. Anything not listed as defended is not defended. This library has had no independent security review.

## Scope of 0.1.0

`Optimizer` and four steps, `ExactCache`, `retry` and `with_fallback`, and `evaluate`. It uses `Scope`, `AuditLog` and the size limits from ragleap-security 0.1.0. It makes no network calls and writes nothing to disk. Not in 0.1.0: model routing, prompt compression, a semantic cache, secret redaction and framework adapters.

## Threats

| # | Threat | What 0.1.0 does | Backed by | Not defended |
|---|--------|-----------------|-----------|--------------|
| 1 | One tenant gets another tenant's cached answer | The cache key is a hash of the exact `Scope` and the whole request; a scope is required for every call; a narrower scope of the same tenant is a different key; `erase(scope)` removes one scope's entries. | `tests/test_cache.py`; 17 mutations | The caller chooses the scope: the cache cannot know whether the caller may use it. The cache is in one process, so it is only as separate as that process. A bug that puts the wrong scope in a call defeats it. |
| 2 | A stale answer is served | Entries expire after `ttl_seconds` (an entry at exactly its expiry is gone); the cache is capped at `max_entries` with least-recently-used eviction. | `tests/test_cache.py` | The cache does not know whether a fact changed before the expiry. A wrong answer that was put in is served until it expires or is erased. Answers that depend on the current time or on context outside the request must not be cached. |
| 3 | A step silently changes what a request means | Each step reports whether it changed the request and whether it lost information; only `trim_tool_output` is lossy, and it marks the cut in the text; `dedupe_context` never touches system or short messages and keeps the last copy; `minify_whitespace` changes spacing only; a step that makes the request longer is discarded; the caller's request is never changed. | `tests/test_steps.py`, `tests/test_optimizer.py`; 35 mutations | A model can still react to spacing or order. Dropping an earlier copy of a repeated message could matter in a conversation where repetition means something. Both are judged by `evaluate` on your cases, not proven. |
| 4 | Oversized input or cache growth exhausts memory | A request is limited in bytes (default 4 MiB) and messages (10,000); the cache is capped; the trim step bounds tool output; `steps` is limited to 50. | `tests/test_optimizer.py`, `tests/test_cache.py` | No limit on the size of a cached value, so `max_entries` times a large value can still be large. No rate limiting. |
| 5 | Retries make an outage worse or never end | Attempts are capped at 10; waits double up to a cap; an optional deadline stops waiting; only the exception classes you list are retried, and never `BaseException`; the failure message names the error types, never their text. | `tests/test_resilience.py`; 13 mutations | Retrying a request that is not safe to repeat (for example one that charges money) is the caller's responsibility. No jitter and no circuit breaker. |
| 6 | Prompts end up in logs | The optional audit record holds counts, step names and the lossy flag, never request text; errors raised here contain no request text. | `tests/test_optimizer.py`, `tests/test_resilience.py`, `tests/test_evaluate.py` | Whatever your own code, your LLM client or the exceptions of your functions log. The audit log is optional. |
| 7 | An evaluation gives a misleading result | No savings figure under 30 cases; failed cases are counted and named, not hidden; every case where the optimized answer scored lower is listed; the quality change comes with a seeded bootstrap interval; the summary says input tokens only. | `tests/test_evaluate.py`; 14 mutations | The judge may be wrong or biased. A test set that does not look like your traffic hides regressions. One run on one set is one sample. The interval does not account for judge noise. |
| 8 | Token counts are wrong | The default count is a conservative estimate; a custom counter is used everywhere and its results are checked (a non-negative int). | `tests/test_model.py`, `tests/test_optimizer.py`; 5 mutations | A real tokenizer will give different numbers. Costs are not computed from tokens here. |
| 9 | A step or counter that raises, returns junk, or is badly written | Steps must come from `ragleap_optimizer.steps` (they carry a name); a bad counter result raises `InvalidRequestError`; a step that edits its input in place cannot change the caller's request. | `tests/test_optimizer.py` | A step you write yourself is trusted code and can do anything inside your process. |

## Not covered in 0.1.0

Model routing and cascades, prompt compression, semantic caching, secret and personal-data redaction before a request leaves your process, a disk or shared cache, and framework adapters. Savings on real traffic have not been measured.

## How this document is checked

`python scripts/mutation_check.py` breaks one protection at a time in a temporary copy and requires a test to fail. All 84 listed mutations were killed when this version was written (Python 3.12, author's sandbox; the Python 3.10 run is still to be done). A killed mutation shows a protection is exercised by a test; it does not show the protection is sufficient.
