# Changelog

All notable changes to `ragleap-optimizer` are documented here. Format
loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.1] - 2026-11-16

Design: #1423. Per-tenant budgets for `ExactCache`. Found in a multi-tenant audit of the libraries.

### The gaps this fixes (both reproduced on 0.1.0)

- One global least-recently-used order let one tenant evict everyone else: with `max_entries=10`, tenant acme cached 3 entries, tenant globex then wrote 30, and acme kept 0 of its 3. This was a fairness and availability gap, not a confidentiality gap: the key already includes the scope, and answers were never shared between tenants.
- There was no cap on value size. `max_entries` counts entries, not bytes: one tenant stored 5 entries of 40 MB each and the process memory grew by about 191 MB.

### Behaviour changes (read first)

- **Eviction order.** When the whole cache is full, the default `eviction="fair"` removes the least recently used entry of the scope that holds the most entries, so a noisy tenant evicts itself first. `eviction="lru"` is the 0.1.0 order.
- **A default value limit.** `max_value_bytes` is 1 MiB by default. A bigger value is not stored (`put()` returns `False`), and because sizes are measured as JSON, a value that is not JSON serializable now raises `InvalidRequestError`. Pass `max_value_bytes=None` to get the 0.1.0 behaviour (any copyable value, no size measured).
- `put()` returns `True` or `False` instead of `None`. A caller that ignores the result is unchanged.

### Added

- `max_entries_per_scope`: a scope at its limit loses its own least recently used entry first.
- `max_bytes_per_scope` and `max_bytes`: limits on the JSON size of the values of one scope and of the whole cache. A value bigger than a limit that applies to it is refused and nothing is evicted for it. By bytes, the scope holding the most bytes loses its oldest entry first, and the entry just stored is never the one removed.
- `stats(scope)`: entries, bytes, hits and misses of exactly that scope. It never shows another scope. `erase(scope)` now also clears that scope's counters.

### Differences from the design in #1423

- The 1 MiB default for `max_value_bytes` and the `"fair"` default were open questions in the issue; both are set as the issue recommended.
- Expired entries are removed when they are read or evicted, so until then they count against the limits.

### What this does not defend

- The cache lives in one process; nothing is shared or written to disk.
- Bytes are measured as JSON length, not as Python object size, so real memory use is higher. The limits are a guard, not an exact bound.
- A scope limit does not limit CPU use or how often a tenant calls `get()` or `put()`. A per-tenant rate limit from ragleap-security can sit in front. No independent security review has happened.

### Verified (real output)

- 217 tests pass (156 from 0.1.0 and 61 new) on Python 3.12.3 and on Python 3.10.20 in the author's sandbox. Both reproductions are tests. With this code, tenant acme keeps 3 of 3 entries while tenant globex writes 30, and the 40 MB values are refused (nothing is stored, the process does not grow).
- `scripts/mutation_check.py`: 119 of 119 mutations killed on both Python versions (the 84 from 0.1.0, four of them adapted to the rewritten cache because their anchors moved, plus 35 new). The first run found one survivor (a hit did not refresh recency in `eviction="lru"` mode, untested because the default order does not use it); it got its own test.

### Not verified

- GitHub CI and the VPS (checked in the pull request). Several processes sharing a cache. Real traffic.

## [0.1.0] - 2026-11-05

First version (design: #1164). There are no behaviour changes: this is a new package.

### Added

- `Optimizer` with a per-step `Report` (tokens before and after, whether the request changed, whether information was lost). A step that makes a request longer is discarded; the caller's request is never changed.
- Steps: `dedupe_context`, `minify_whitespace`, `stable_prefix` and `trim_tool_output`. Only `trim_tool_output` can lose information, and it reports `lossy=True` when it cuts. (The design issue first listed trimming as lossless; that was wrong and is corrected there.)
- `ExactCache`: in-memory, scope required, whole request in the key, expiry, least-recently-used eviction, erase per scope.
- `retry` and `with_fallback`: bounded attempts, doubling waits with a cap, a deadline, and an error that names the failures without their text.
- `evaluate`: paired comparison on your own cases with a seeded bootstrap interval; no savings figure below 30 cases; regressions listed by case.
- Threat model with 9 rows; `scripts/mutation_check.py` with 84 mutations.

### Verified

- 156 tests pass and all 84 mutations are killed in the author's sandbox (Python 3.12) and on the VPS (Python 3.10.12, run by Pushpa). In the sandbox the mutation check was run three times with the same result; one test that caught a mutation only by chance was replaced by a deterministic one.
- CI on pull request #1183: all 52 checks passed, including `ragleap-optimizer-tests` on Python 3.10, 3.11 and 3.12 and the Plugin Security Scan.
- Built the way the release job builds (`python -m build`) and installed the wheel into a clean venv (with `ragleap-security` 0.1.0 from PyPI): the README example ran, including the optimizer, the cache, retry, fallback and `evaluate`. The wheel holds only the package modules plus metadata.
- A lossless pipeline never made a request longer on 200 random requests.

### Not verified

- The package as published on PyPI: checked only after the release (see the GitHub Release notes).
- Any speed or token-saving number on real traffic. No benchmark has been run; the library makes no savings claim.
- Token counts against any real tokenizer.
- Behaviour with message shapes other than the OpenAI-style `messages` list (for example other providers' content blocks beyond text parts).
- Several processes sharing a cache (the cache is per process).
- No independent security review has been done.
