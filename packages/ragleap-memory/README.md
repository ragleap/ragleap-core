# ragleap-memory

Durable, tenant-scoped long-term memory for LLM applications. Alpha. **Not independently audited**: see `docs/threat-model.md` for exactly what is and is not defended. Single-tenant use is simply one fixed `Scope`.

## What is in 0.2.0

- `SQLiteStore`: one SQLite file (or memory), FTS5 keyword search, no server. Needs SQLite built with FTS5; the store refuses to start without it.
- Every call takes a `Scope` (from ragleap-security) and is filtered by it inside the SQL. A tenant-wide scope sees its whole tenant; a narrower scope sees only its own subject, agent and session.
- Typed entries (`episode`, `fact`, `summary`) with a trust label (`untrusted`, `model`, `owner`). New entries default to `untrusted`.
- `supersede` for facts that change (history is kept), `forget`, `export`, `erase` (removes the text from the file), `sweep_expired`, and an optional `AuditLog` that records operations without entry text.
- New in 0.2.0: a **token budget** on search (`max_tokens`), **usage counters** and **quotas** per tenant, `ShardedStore` (one SQLite file per tenant), and **download and upload** of a memory (`export_file`, `import_file`). See below. Read the behaviour changes in `CHANGELOG.md` before upgrading: a 0.1.0 file is migrated in place on first open.

```python
from ragleap_memory import SQLiteStore
from ragleap_security import Scope

store = SQLiteStore("memory.db")
alice = Scope("acme", subject="alice")

old = store.add(alice, "Alice prefers email", trust="owner")
new = store.supersede(alice, old.id, "Alice prefers phone calls", trust="owner")

for hit in store.search(alice, "how should we contact alice", k=3, min_trust="model"):
    print(hit.entry.trust, hit.entry.text)
```

The store labels trust but does not decide what to do with retrieved text. A consumer that puts memory into a prompt must treat anything below `owner` as untrusted input.

## Keeping token use predictable (0.2.0)

```python
hits = store.search(alice, "contact preference", k=5, max_tokens=300)   # never more than 300 estimated tokens
print(store.usage(alice))   # entries, stored_tokens, and what searches served today
```

- `max_tokens` is a hard ceiling on the estimated tokens of the entries a search returns, however large the store grows. Candidates are taken in rank order; an entry that does not fit what is left is skipped and the next one is tried.
- Token counts are an **estimate**: one token per 3 UTF-8 bytes, rounded up (it over-counts English, which is about 4 bytes per token). It is not any model's real tokenizer. Pass `token_estimator=` to the store to use your own; it must return a positive int.
- The budget bounds tokens, **not time**. A search over a very large store still has to do the work of finding candidates.
- `quota_entries` and `quota_tokens` (per tenant, off by default) refuse a write that would pass the limit with `QuotaExceededError`. A write exactly at the limit is allowed. `supersede` needs a free slot because the old entry is kept. This limits what a tenant can store; it does not limit what a *model* costs you: that depends on what your application sends.
- `usage(scope, period=None)` returns what the scope stores now and what searches served on one UTC day. Served counters are kept per tenant, not per narrower scope, and a tenant-wide `erase` removes them.

## Download and upload (0.2.0)

```python
store.export_file(alice, "alice-memory.jsonl")                  # download: one JSON Lines file
result = other_store.import_file(alice, "alice-memory.jsonl")   # upload: continue from it somewhere else
print(result)   # ImportResult(imported=..., skipped_duplicates=..., dropped_links=...)
```

`import_stream(scope, binary_file_object)` does the same for an uploaded file. The file format is JSON Lines: a header line (`format`, `version`, `count`) and one entry per line, exactly the fields `export()` returns. An uploaded file is **untrusted input**, so an import is strict:

- Every entry is checked like a new `add` (kind, size, metadata, importance, dates). One bad entry, or a quota, stops the whole import and nothing is imported (one transaction). The error names the line number, never the text.
- Trust labels are **capped**: by default every imported entry becomes `untrusted`, whatever the file says. Pass `max_trust="model"` or `"owner"` only when you know where the file came from.
- Every entry must lie inside the scope you import into. A file from another tenant is refused, unless you pass `rescope=True`, which moves the file into your tenant (narrower parts such as the subject are kept).
- Entries always get new ids. Supersede and derived links are remapped; links that point outside the file or across scopes are dropped and counted.
- An entry with the same scope, kind and text as one already stored is skipped, so importing the same file twice adds nothing (`skip_duplicates=False` turns this off).
- Limits: 100,000 entries, 64 MiB and 64 KiB per line by default (`max_entries`, `max_bytes`).

An export file is **plain text with mode 0600**: anyone who can read it can read the memory. There is no encryption in this library yet (planned with ragleap-security `crypto`). `export_file` refuses to overwrite an existing file. Import does not check *who* may upload: that is your application's job.

## One file per tenant (0.2.0)

```python
from ragleap_memory import ShardedStore

store = ShardedStore("memory-dir", max_open=64, quota_entries=100_000)
```

`ShardedStore` has the same methods as `SQLiteStore` (`compact` takes a scope) and routes by `scope.tenant`. Each tenant gets its own file named by a hash of the tenant id; the directory is set to mode 0700 and files are 0600. A tenant-wide `erase` deletes the tenant's files. At most `max_open` files stay open (least recently used are closed and reopen on demand). Options such as the audit log, quotas and `token_estimator` are passed to every shard, so quotas apply per tenant. Separate files stop a large tenant from slowing a small one's index; they are **not** a stronger security boundary than the scope filter, because both live in the same directory with the same file permissions.

## Verifying it yourself

```
pip install -e ../ragleap-security -e ".[test]"
pytest
python scripts/mutation_check.py   # breaks one protection at a time; every mutation must be killed
```

## Not in 0.2.0

Embeddings and vector search (planned for 0.3.0), consolidation and retention policies, importance-based ranking, Postgres, encryption at rest, a review queue and the ragleap-agents adapter are planned (issue #900). Search quality has not been measured on any benchmark.
