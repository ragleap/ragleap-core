# Changelog

All notable changes to `ragleap-memory` are documented here. Format
loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.3.1] - 2026-11-12

Design: #1294. Encrypted export and import. No schema change. No new cryptography: every byte is encrypted and decrypted by `ragleap_security.crypto` (AES-256-GCM); this package only frames the file.

### Behaviour changes (read first)

- Plain export files (format version 1) and every call without the new arguments behave exactly as in 0.3.0.
- `import_file` and `import_stream` now **refuse an encrypted file (format version 2) when no key or passphrase is given**. Before 0.3.1 such a file did not exist.
- A file whose header says `version` 2 is now read as an encrypted file. In 0.3.0 it was refused as an unsupported version; it is still refused, with a different message, unless you give a key or passphrase.
- Passing `decrypt_with` or `passphrase` **refuses a file that is not encrypted** (no downgrade: a plain file cannot take the place of an encrypted one).
- The test extra now includes `cryptography`. A new `crypto` extra installs `ragleap-security[crypto]`. Without it nothing else changes, and asking for encryption raises `MemoryStoreError` that names the extra.

### Added

- `export_file(scope, path, *, encrypt_with=None, passphrase=None, context=None)`. Give a `Key`, a `KeyRing` (its active key is used) or a passphrase, never both. Same rules as a plain export: mode 0600, the file must not exist, a failed write removes the partial file. The audit event `memory.export` gets `"encrypted": true`.
- `import_file(..., decrypt_with=None, passphrase=None, context=None)` and the same arguments on `import_stream`. `ShardedStore` passes them through. The audit event `memory.import` gets `"encrypted": true` for an encrypted file; the line for a plain file is unchanged.
- Export format version 2: line 1 is a plain JSON header (`format`, `version` 2, `count`, `file_id` of 16 random bytes, `kdf` of `none` or `scrypt` with its settings and a salt); every other line is one base64 envelope holding one entry. With a passphrase the key is derived once per file (scrypt 2^15, r 8, p 1), not once per line.
- Each line is bound to its place: the authenticated context is a fixed label, the SHA-256 of the exact header line, the line's position and your own `context` (at most 1,024 bytes). A line that is moved, duplicated or taken from another file, any changed header byte, a wrong key, a wrong context and any changed byte in a line all fail to decrypt. Dropped and extra lines fail the count check.
- Import stays the single safe path: the lines are decrypted by a small reader that hands the unchanged import code a plain version 1 stream, so the trust cap, the scope check, quotas, the one-transaction rule and every limit still apply. A decrypted file is not a trusted file.
- `ragleap_memory.exportcrypto`: the file layout in one small module.

### Differences from the design in #1294

- The authenticated context holds a hash of the whole header line instead of `file_id` and `count` separately. The header holds both, so a changed `file_id` or `count` still fails every line, and now every other header byte is authenticated too.
- Only canonical base64 is accepted (Python's decoder alone accepts a changed padding bit, so a flipped bit could go unnoticed), and every line must end with exactly one newline. Blank lines are tolerated, as in plain files.
- Limits: an encrypted line may be up to 90,000 bytes; the file on disk may be up to twice `max_bytes`; the decrypted data obeys `max_bytes`, `max_entries` and 64 KiB per line as before.
- A header that says more entries than `max_entries` is refused before anything is decrypted.
- Wrong key, wrong context and damaged line all give the same message with the line number (`line 7: cannot decrypt`); a header or count problem gives a different, specific message. Messages never contain entry text.

### What this does not defend

- It protects the export FILE. The SQLite database is still unencrypted at rest, and so is the memory of the process while it exports or imports.
- The header shows the number of entries, and the length of each line shows roughly how long the entry is. The header of an empty export is not authenticated: there is nothing in the file to protect.
- Someone who holds the key can read and forge a file. An old file put back in place of a newer one is not noticed (no freshness).
- A decrypted file is still untrusted input: trust labels are capped on import exactly as before.
- No independent security review has happened.

### Verified (real output)

- 532 tests pass (472 from 0.3.0 and 60 new) on Python 3.12.3 and on Python 3.10.20 (author's sandbox; SQLite 3.45.1 and 3.53.1; cryptography 50.0.2). They also pass with cryptography 42.0.0, the lowest version allowed. In an environment without cryptography, 476 pass and the encrypted-file tests are skipped.
- 232 of 232 mutations killed on both Python versions (177 from 0.3.0 and 55 new). One candidate mutation was dropped as equivalent (making the newline optional in the line pattern changes nothing, because a last line without its newline loses a character and fails to decode anyway). One existing test case changed: it used `version: 2` as its example of an unsupported version, which is now the encrypted format, so it uses version 3.
- Every single bit changed anywhere in a small encrypted file (header and lines) is refused and imports nothing; the same for a sample of positions in a passphrase file.
- A plain export file written by the released ragleap-memory 0.3.0 from PyPI imports with this code. The released 0.3.0 refuses a 0.3.1 encrypted file with the "unsupported version" message instead of misreading it.
- The README example was run as written.

### Not verified

- The VPS (Python 3.10.12, SQLite 3.37.2) is checked in the pull request, not here. Windows and macOS were not run.
- Several processes writing or exporting one file, and network file systems.
- Timing and memory with very large files: an import decrypts into memory up to `max_bytes` (64 MiB by default).
- The mutation run on the VPS.

## [0.3.0] - 2026-11-09

Correction (2026-11-09): the published 0.3.0 package shows 2026-11-08 here, the day this entry was written. It was released on 2026-11-09 (UTC).

Design: #1263. Vector and hybrid search over vectors that the caller's own embedder function produces. The library has no provider code, no keys and no network access.

### Behaviour changes (read first)

- A 0.1.0 or 0.2.0 database file is **migrated in place to schema 3 on first open** (one transaction). A migrated file cannot be opened by 0.2.0 or older, which refuse newer schemas. Back up the file first if you may need to go back.
- `search` has two new keyword arguments, `mode` and `query_vector`. The default, `mode="keyword"`, behaves exactly as in 0.2.0.
- `usage()` dictionaries have two new keys, `embedded_tokens` and `embed_calls`.
- Entries removed by `forget`, `erase` or `sweep_expired` now lose their vector too (a database trigger).
- Code that does not pass an embedder sees nothing else change.

### Added

- Options on `SQLiteStore` and `ShardedStore`: `embedder`, `embedding_model`, `embedding_dim`, `embed_quota_tokens`, `embed_batch_size` (default 64) and `max_vector_candidates` (default 5,000). Model and size go together; an embedder needs both.
- `embed_pending(scope, limit=100, dry_run=False, replace_stale=False)` returns `EmbedResult(embedded, tokens, calls, remaining, dry_run)`. It embeds entries the scope can see that have no vector for the store's model (most recent first; superseded and expired entries are skipped). `dry_run=True` changes nothing and calls nothing. `replace_stale=True` also re-embeds vectors made by another model or size. It is also available on `ShardedStore`.
- `search(..., mode="vector" | "hybrid", query_vector=None)`. Vector mode ranks by cosine similarity and scans at most `max_vector_candidates` entries (newest first, after every filter). Hybrid fuses the keyword and vector ranks by reciprocal rank fusion (k=60); an entry without a vector can still be found by keyword. `kinds`, `min_trust`, superseded and expired entries, `k` and `max_tokens` work in every mode. A query embedding is one embedder call; `query_vector` avoids it.
- A per-tenant daily cap, `embed_quota_tokens` (off by default). Embedder tokens and calls are counted **before** each call (a call that then fails stays counted) and shown in `usage()`. If a batch would pass the cap, only the entries that fit are embedded and `remaining` says how many are left; if not even one fits, `QuotaExceededError` is raised before the embedder is called. Exactly at the cap is allowed.
- `EmbedderError`: the embedder raised, or returned the wrong number of vectors, wrong sizes, non-finite numbers or an all-zero vector. Its message never contains entry text or vectors, and nothing from that batch is stored.
- Schema 3: an `embeddings` table (vectors stored normalized as little-endian float32), a delete trigger, and the two new usage counters.
- Audit event `memory.embed` (count and tokens, never text).

### Differences from the design in #1263

- `EmbedResult` has the fields `embedded`, `tokens`, `calls`, `remaining` and `dry_run`; the design listed `skipped` instead of `remaining`.
- When only part of a batch fits under the cap, `embed_pending` embeds what fits and reports `remaining`; it raises only if not even one entry fits. The design said it would raise.

### Verified (real output)

- 472 tests pass (306 from 0.2.0 and 166 new) on Python 3.12.3 and on Python 3.10.20 (author's sandbox; SQLite 3.45.1 and 3.53.1).
- 177 of 177 mutations killed on both Python versions. A first full run found one survivor (the `kinds` filter of the keyword part of hybrid search was not tested on its own); it has its own test now.
- A database written by the released ragleap-memory 0.2.0 from PyPI opens with this code, migrates to schema 3, keeps its usage counters, and can then be embedded and searched. Each tenant only got its own vectors.
- `erase` removes the vector bytes from the file (byte-level test with a positive control); export files carry no vectors and an import refuses an entry that carries one.

### Not verified

- SQLite 3.37.2 (the VPS) is checked in the pull request, not here. Windows and macOS were not run.
- Big-endian machines: vectors are stored little-endian with a byte swap that has never been run.
- No real embedding provider was used; the tests use a deterministic fake embedder. Retrieval quality has not been measured and depends on the embedder.
- The scan timings in the threat model (128 ms and 470 ms) were measured once in a sandbox.
- Several processes writing one file and network file systems are still not verified.

## [0.2.0] - 2026-11-02

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
- CI on pull request #1107: all 49 checks passed, including `ragleap-memory-tests` on Python 3.10, 3.11 and 3.12 and the Plugin Security Scan.

### Measured (one run on the VPS, 2026-11-02)

AMD EPYC 7543P (1 core reported), Python 3.10.12, SQLite 3.37.2, load average 0.14. Synthetic text (12 words per entry, a Zipf-like 5,000-word vocabulary), 3-word queries, 200 queries per case, `k=50`. Every search also writes a usage counter: in a separate check on 10,000 entries that write cost about 2 ms of an 11.7 ms median.

- A 300-token budget held in every case (the largest total served was 300 tokens). Without a budget a search served 859 to 1,057 tokens on average. The budget does not make a search faster.
- One tenant, one file: 10,000 entries median 20.6 ms, p95 47.5 ms; 100,000 entries median 89.9 ms, p95 253.7 ms.
- Hard case (only 30 distinct words, so nearly every entry matches): 100,000 entries median 242.7 ms, p95 331.0 ms.
- 100 tenants, 100,000 entries in total, one tenant queried: one shared file median 28.9 ms (p95 46.8 ms); `ShardedStore` median 5.4 ms (p95 9.8 ms); `ShardedStore` with queries rotating over all 100 tenants and `max_open` 64: median 15.2 ms (p95 24.3 ms).
- Adding entries one at a time (one transaction each): about 290 to 510 per second.
- Numbers varied between runs on the same machine (a 10,000-entry case measured 20.6 ms median in the full run and 11.7 ms in a later separate run), so treat them as orders of magnitude, not as a promise.

### Not verified

- Import of very large files (the file is read in full before it is written; the limits bound it), and any import from tools other than this library's own export.
- Speed beyond the single run above: 1,000,000 entries, other hardware or disks, real text instead of synthetic text, several processes, and the speed of `import_file`.
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
