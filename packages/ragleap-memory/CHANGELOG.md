# Changelog

All notable changes to `ragleap-memory` are documented here. Format
loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.2.0] - unreleased

Design: #900 (comment of 2026-10-25). Pushpa's requirements: low and predictable token use, no bill spike however large the memory, strongly indexed separate compartments.

### Behaviour changes (read first)

- `search` is no longer read-only: it records the tokens it served in a short write transaction. A store opened read-only will fail on `search`.
- A 0.1.0 database file is **migrated in place on first open** (one transaction). A migrated file cannot be opened by 0.1.0, which refuses newer schemas. Back up the file first if you may need to go back.
- SQLite 3.24 or newer is required (upserts), as well as FTS5. The store refuses to start otherwise.
- `MemoryEntry` and `SearchHit` have a new `tokens` field, and `export` dictionaries have a new `"tokens"` key.

### Added

- `search(..., max_tokens=N)`: a hard budget on the estimated tokens returned. An entry that does not fit is skipped and the next one tried, so the total never exceeds the budget at any store size. It bounds tokens, not time.
- `estimate_tokens` (one token per 3 UTF-8 bytes, at least 1; an estimate that over-counts English) and a `token_estimator=` option.
- `usage(scope, period=None)`: entries and estimated tokens stored, and tokens and searches served per tenant per UTC day.
- `quota_entries` and `quota_tokens` (per tenant, off by default) and `QuotaExceededError`. Exactly at the quota is allowed; `supersede` counts as a write.
- `export_file` and `import_file` / `import_stream`: download and upload of a memory as a JSON Lines file (header with format, version and entry count). An import is strict and all-or-nothing: entries are checked like `add`, trust is capped by `max_trust` (default `untrusted`), entries must lie inside the import scope (`rescope=True` to move a file into your tenant), ids are new and links are remapped, duplicates are skipped, and there are limits on entries, bytes and line length. An export file is plain text (mode 0600, never overwrites).
- `ShardedStore`: one SQLite file per tenant (named by a hash of the tenant id; directory 0700, files 0600), least-recently-used shards closed beyond `max_open`, and a tenant-wide `erase` that deletes the files.
- Threat model rows 10 (bill spike), 11 (shared index) and 12 (hostile upload file). `scripts/mutation_check.py` now has 84 mutations (27 from 0.1.0, 57 new).

### Verified

- 306 tests pass in the author's sandbox (Python 3.12, SQLite 3.45.1): the 89 tests of 0.1.0 unchanged plus 217 new ones (many are parameterised). All 84 mutations are killed there. Two things were found by tests or mutations along the way: a race when a shard is evicted between lookup and use, and an import header with `"version": true` that was accepted because `True == 1` in Python; both are fixed and tested.
- A hand-built 0.1.0 database migrates, keeps its entries and search, and its totals feed the quota. A migration that fails half way leaves the file as 0.1.0 wrote it.
- On the VPS (Python 3.10.12, SQLite 3.37.2), run by Pushpa: 306 tests pass and all 84 mutations are killed.

### Not verified

- The CI matrix (3.10, 3.11, 3.12): not run yet when this was written.
- Import of very large files (the file is read in full before it is written; the limits bound it), and any import from tools other than this library's own export.
- Any performance number. No benchmark has been run on the VPS. Earlier numbers from a different machine are not published here.
- Windows, network file systems, several processes writing one file, power-loss durability, and more than a few threads on `ShardedStore`.
- Retrieval quality and multilingual precision (unchanged from 0.1.0). Token estimates against any real tokenizer.
- No independent security review has been done.

## [0.1.0] - 2026-10-24

Note: the CHANGELOG inside the published 0.1.0 files says 2026-10-23, the day it was prepared. The tag was pushed on 2026-10-24 and the date was corrected here afterwards.

First version (design: #900). It replaces an untracked prototype that was never committed, so there are no behaviour changes.

### Added

- `SQLiteStore`: durable memory in one SQLite file with FTS5 keyword search.
- `add`, `get`, `search`, `supersede`, `forget`, `export`, `erase`, `sweep_expired`, `compact`, `count`, all scoped by a ragleap-security `Scope` inside the SQL.
- Typed entries (`episode`, `fact`, `summary`) with trust labels (`untrusted` by default), expiry, and links to the entries they were derived from.
- Optional audit log (ids and counts only, written inside the transaction).
- `docs/threat-model.md` and `scripts/mutation_check.py` (27 mutations).

### Verified

- 89 tests pass on Python 3.10 (the VPS, SQLite 3.37.2) and on Python 3.12 (the author's sandbox). The CI test job passes on Python 3.10, 3.11 and 3.12 (#922). All 27 mutations are killed on Python 3.10 and 3.12.
- After `erase()`, a byte-level search of the database file and its journal finds no trace of the deleted text (tested on the two SQLite builds above).

### Not verified

- Windows, network file systems, several processes writing one file, and power-loss durability.
- Retrieval quality: no benchmark has been run. Multilingual precision is unmeasured (whole-word search is tested for a few scripts).
- The published wheel had not been installed from PyPI when this was written (the GitHub Release records that check).
- No independent security review has been done.
