# Changelog

All notable changes to `ragleap-vectorstores` are documented here. Format
loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.7.0] - 2026-11-12

### Added
- Tenant filtering: `tenant_id` is now a filterable field in `RedisBackend` (TAG field), `OpenSearchBackend` (keyword, also mapped on existing indexes) and `LanceDBBackend` (nullable column, added to existing tables). Chunks without a tenant tag never match a `tenant_id` filter.

### Changed
- `RedisBackend` and `OpenSearchBackend` now raise `ValueError` for filter keys they cannot filter on. Before, Redis ignored them (so a filter such as `{"tenant_id": ...}` searched every tenant) and OpenSearch silently matched nothing. `LanceDBBackend` and `UpstashBackend` validate filter keys too.
- A Redis index created before this change has no `tenant_id` field: filtering by `tenant_id` on it raises `RuntimeError`; use a new `index_name`.

### Fixed
- `ChromaBackend.insert_chunk`: caller metadata can no longer overwrite `document_id`, `document_name`, `chunk_index` or `token_count`.

## [0.6.0] - 2026-10-22

### Upgrade note
- Score thresholds: `RedisBackend`, `ChromaBackend` and `LanceDBBackend` now return `similarity_score` on the 0 to 1 scale (see Fixed). If you filter or alert on those scores, re-check your thresholds.

### Fixed
- `ChromaBackend.search_dense()` and `LanceDBBackend.search_dense()` now return `similarity_score` on the same 0 to 1 scale as the other backends: `1 - distance / 2` (clamped), so identical vectors score 1.0, orthogonal 0.5 and opposite 0.0. Before they returned `1 - distance`, the raw cosine in [-1, 1] (0.0 for orthogonal, -1.0 for opposite), the same inconsistency as issues 577 and the Redis fix. Behavior change: returned scores change; stored data is not affected. Docstrings that described the old convention were updated.
- `RedisBackend.search_dense()` now returns `similarity_score` on the same 0 to 1 scale as the other backends: `1 - distance / 2` (clamped), so identical vectors score 1.0, orthogonal 0.5 and opposite 0.0. Before it returned `1 - distance`, the raw cosine in [-1, 1] (0.0 for orthogonal, -1.0 for opposite; live-verified on Redis Stack 7.4.7), so a caller filtering on `similarity_score` behaved differently per backend (same kind of inconsistency as issue 577). Behavior change: returned scores change; stored data is not affected.
- `UpstashBackend.insert_chunk()`: caller metadata can no longer overwrite the stored `document_id`, `document_name`, `chunk_index`, `token_count` or `text` (a key with one of those names used to replace the real value). Behavior change only for metadata that used those key names; other keys are stored as before.
- `UpstashBackend.list_documents()` now counts all chunks of a document: it follows the `range()` cursor until the prefix scan ends. Before, a single call capped `chunk_count` at 1000. Costs one extra `range()` request per further 1000 chunks of a listed document.

### Verified
- The Redis score fix was checked live on a Redis Stack 7.4.7 container and runs in CI against redis-stack; the Chroma and LanceDB fixes against real embedded instances; the two Upstash fixes with mock-based tests only (no real Upstash index); the OpenSearch and Upstash module changes in the score fix are comment edits.

## [0.5.2] - 2026-10-19
### Changed
- Project links in the package metadata (Homepage, Repository, Issues) and the roadmap link in the
  README now point at https://github.com/ragleap/ragleap-core, the repository's new home in the
  ragleap organization. Metadata only, no code change. (The 0.5.1 project page on PyPI still shows
  the old owner; GitHub redirects the old links.)
### Verified
- The only commit touching this package since the 0.5.1 tag is that link update, so the code is
  identical to 0.5.1 apart from the version string.

## [0.5.1] - 2026-09-21
### Fixed
- REAL BUG in v0.5.0, reproduced with the published v0.5.0 wheel on Python
  3.10.20: `OpenSearchBackend.insert_document()` raised `AttributeError:
  module 'datetime' has no attribute 'UTC'`. `datetime.UTC` only exists on
  Python 3.11+, but this package declares support for Python 3.10. Fixed by
  using `datetime.timezone.utc`, which works on every supported version and
  produces the identical timestamp. On Python 3.10, v0.5.0 could not ingest
  documents into OpenSearch; Python 3.11+ was unaffected, and the other
  methods do not use the affected call.

### Correction
- The v0.5.0 entry said the new backend used `datetime.now(datetime.UTC)` as
  "the current, non-deprecated form". That form is not available on Python
  3.10, which this package supports. The v0.5.0 tests ran only on Python 3.14
  and the OpenSearch tests were skipped on the Python 3.10 VPS, so this went
  unnoticed until now.

### Changed
- The Chroma, LanceDB, Redis and Upstash backends no longer call the
  deprecated `datetime.datetime.utcnow()` (a DeprecationWarning on Python
  3.12+) - this resolves the known issue listed under v0.5.0. They now use
  `datetime.now(datetime.timezone.utc).replace(tzinfo=None)`, which produces
  exactly the same naive-UTC string as before, so stored data and
  `list_documents()` output are unchanged.

### Added
- `tests/test_datetime_compat.py`: always-run tests that need no live
  infrastructure - an AST scan that fails if any backend uses `utcnow()` or
  `datetime.UTC`, and a mocked-client test of
  `OpenSearchBackend.insert_document()`.
- `REDIS_TEST_URL` environment override in the Redis tests, so they can run
  against a Redis Stack on any port (the previous hardcoded port 6380 can be
  taken by other containers).

### Not re-verified
- The 16 live OpenSearch tests were not re-run for this patch (no OpenSearch
  instance was available); the change touches only the timestamp expression.

## [0.5.0] - 2026-09-21
### Added
- `OpenSearchBackend` - fifth vector backend, via OpenSearch's native k-NN
  vector search. Available via the `opensearch` optional extra
  (`pip install ragleap-vectorstores[opensearch]`).
- Requires a real running OpenSearch instance - `opensearch_url=` is
  mandatory, there is no embedded/local mode (unlike Chroma/LanceDB).
- Unlike RedisBackend's SQLite sidecar, the document registry is a second
  real OpenSearch index (`<index_name>_documents`), so sorting and paging
  use OpenSearch's own query DSL.
- Metadata filtering supports arbitrary term/range filters over any mapped
  field (via `bool.filter`) - more flexible than Redis's document_id-only
  limit. Every key in `metadata_filter` is applied: a nonexistent field
  narrows results to zero rather than being silently ignored.
- `init_schema()` is idempotent, and raises a clear `RuntimeError` if an
  existing index has a different vector dimension.
- Writes use `refresh=True` for immediate read-after-write consistency, at
  the cost of indexing throughput.
- `supports_sparse()` reports `False` - OpenSearch supports real BM25/hybrid
  search, but it is not implemented here, so it is honestly reported as
  unsupported. Hybrid search falls back to dense-only.
- 16 new tests, all against a real OpenSearch instance (no mocks). Tests
  skip cleanly (not fail) if `OPENSEARCH_TEST_URL` is not set.

### Live-verified gotchas
- The `nmslib` k-NN engine shown in most tutorials is deprecated and
  REJECTED at index creation on OpenSearch 3.0+ (`mapper_parsing_exception`).
  This backend uses `engine: "lucene"`.
- With `space_type: "cosinesimil"` and the `lucene` engine, the returned
  score is NOT raw cosine similarity. It is `(1 + cosine_similarity) / 2`,
  mapping [-1, 1] onto [0, 1]. Verified against real numbers; used directly
  as `similarity_score`, like Upstash and unlike Chroma/Redis.
- `Indices` is not importable as `from opensearchpy.client import Indices`;
  it is reached as the `.indices` attribute of an `OpenSearch` client.
- `datetime.datetime.utcnow()` is deprecated on Python 3.12+ (a real
  `DeprecationWarning` was observed on Python 3.14). The new backend uses
  `datetime.datetime.now(datetime.UTC)`.

### Known limitations
- No bulk-insert path yet: `insert_chunk()` indexes one document per request.
- `delete_document()` issues a `delete_by_query` on the chunks index plus a
  direct delete on the registry index - two requests, not one atomic
  operation.
- Testing venue: the 16 OpenSearch tests were run against OpenSearch 3.8.0
  in Docker on a local Windows machine (Python 3.14, opensearch-py 3.2.0).
  They have not been run in CI or on Linux.
- KNOWN ISSUE (not fixed in this release): the Chroma, LanceDB, Redis and
  Upstash backends still call the deprecated `datetime.datetime.utcnow()`
  in `insert_document()`. It emits a DeprecationWarning on Python 3.12+.

## [0.4.0] - 2026-09-09
### Added
- `UpstashBackend` - fourth vector backend, via Upstash Vector's managed
  serverless REST API. Available via the `upstash` optional extra
  (`pip install ragleap-vectorstores[upstash]`).
- Unlike Chroma/LanceDB (embedded) and Redis (self-hostable), Upstash
  Vector has no local mode at all - the index (including its fixed
  dimension and dense-vs-hybrid type) must already exist, created via
  the Upstash console. `init_schema()` cannot create anything; it
  verifies the real index's dimension and type via `info()` and raises
  a clear error on mismatch, live-verified against a real index rather
  than assumed.
- LIVE-VERIFIED GOTCHA: an Upstash index can be created as "dense" or
  "hybrid" (requiring sparse vectors on every upsert) - a hybrid index
  rejects dense-only upserts with a confusing error. `init_schema()`
  checks this via `info()` and fails clearly up front instead.
- Metadata filtering is a genuine strength here versus the other
  backends: Upstash's `filter=` is a real SQL-like string over an
  actual JSON metadata dict, so arbitrary multi-key filters work
  natively - closer to Chroma's flexibility than Redis's
  document_id-only limitation, without Chroma's `$and` wrapping either.
- `similarity_score` is used directly from Upstash's `query()` - its
  score is already a normalized similarity in [0, 1] (live-verified: an
  exact match returns 1.0), unlike Chroma/Redis which return a distance
  requiring `1 - distance` conversion.
- `delete_document()` and chunk-counting use Upstash's native `prefix=`
  parameter on `delete()`/`range()` - a genuine prefix-scan, simpler
  than Redis's manual scan-then-delete-by-key-list pattern.
- `supports_sparse()` reports `False` - Upstash Vector does support
  real hybrid dense+sparse search, but that requires a hybrid-type
  index and sparse embeddings this package doesn't generate, so it's
  honestly reported as unsupported.
- 14 new tests, all against a real Upstash Vector index (no mocks).
  Tests skip cleanly (not fail) if `UPSTASH_VECTOR_REST_URL`/`TOKEN`
  aren't set in the environment.

## [0.3.3] - 2026-09-06
### Fixed
- REAL BUG, live-verified: RedisBackend's key_prefix used to default to
  the same literal string ("ragleap:chunk:") regardless of index_name.
  Two RedisBackend instances with different index_name but both left
  at the default key_prefix silently shared the same Redis keyspace -
  creating the second index caused RediSearch to auto-index the first
  instance's leftover chunks too, and a fresh KNN query could return
  stale data from a completely unrelated instance. Reproduced live:
  three separate verification runs with different index_names, all
  writing into the same "ragleap:chunk:*" keyspace, and a top_k=1
  query on the newest instance returned a chunk from an unrelated
  earlier run. Fixed by deriving the default key_prefix from
  index_name, so two different indexes never collide unless the
  caller explicitly passes the same key_prefix on purpose. Added a
  dedicated regression test (two instances, different index_name, no
  explicit key_prefix, each must only ever see its own chunks) - this
  test fails against the old code and passes against the fix.

This is a data-isolation fix, not just a docs fix: anyone running two
RedisBackend-backed RagLeap deployments against the same Redis server
without an explicit key_prefix was at risk of their data silently
mixing. Given the package is still alpha and RedisBackend shipped only
days ago, fixing the default now (before wider adoption) was judged
better than leaving the footgun in place.

## [0.3.2] - 2026-09-06
### Fixed
- README's "Available backends" table was missing a Redis row entirely
  (only Chroma and LanceDB were listed) - this table is separate from
  the Usage section's code examples, which were already fixed in
  v0.3.1. Caught by checking the live rendered PyPI page, same as the
  v0.2.1 and v0.3.1 doc-lag issues. Added the Redis row with the same
  level of detail as the other two (extra name, server requirement,
  metadata filtering limitation, sparse-search status).
- REAL REGRESSION, live-verified: init_schema()'s MODULE LIST parsing
  assumed a single fixed response shape (dict with bytes keys), but
  running the full test suite in a different environment reproduced
  the older flat-list shape again - same exact client construction,
  same server instance, same redis-py version, different shape on
  different occasions. Almost certainly RESP2/RESP3 protocol
  negotiation happening beneath redis-py rather than anything this
  code controls. Parsing is now defensive against both observed
  shapes rather than assuming either one generalizes; verified across
  10 consecutive clean test runs plus two full-suite runs (40/40 both
  times) before shipping this fix.

## [0.3.1] - 2026-09-05
### Fixed
- README's Usage section was missing a RedisBackend example (only
  Chroma/LanceDB were shown) - same category of gap as the v0.2.1
  missing-uv-add-lines fix, caught the same way: checking the real
  rendered PyPI page after publish rather than assuming the README
  was complete.
- pyproject.toml's keywords only listed generic terms (rag,
  vector-search, vector-database, ragleap) - added per-backend terms
  (chroma, chromadb, lancedb, redis, redisearch) plus embeddings and
  similarity-search for PyPI/search discoverability.

## [0.3.0] - 2026-09-05
### Added
- `RedisBackend` - third vector backend, via RediSearch/Redis Stack's
  HNSW vector index over a real Redis connection. Available via the
  `redis` optional extra (`pip install ragleap-vectorstores[redis]`).
  Unlike Chroma/LanceDB, this backend requires a real running server -
  plain Redis has no vector search; it must be Redis Stack, or plain
  Redis with the RediSearch module loaded. init_schema() checks
  MODULE LIST and raises a clear RuntimeError immediately if 'search'
  isn't present, rather than failing confusingly later at query time.
- KNOWN LIMITATION, documented honestly: RediSearch requires fields to
  be declared in the index schema up front, unlike Chroma's flexible
  `where=`. search_dense()'s metadata_filter only narrows results by
  `document_id` (a declared TAG field) - additional keys are accepted
  but ignored rather than raising, same 'don't claim a capability that
  isn't there' spirit as supports_sparse().
- `supports_sparse()` correctly reports `False` for Redis - RediSearch
  does support real full-text search via TextField/BM25, but that
  surface wasn't implemented or tested in this release.
### Fixed
- TAG field values (e.g. UUID document_ids containing hyphens) must be
  escaped before use in RediSearch query strings - live-verified that
  an un-escaped hyphenated document_id silently returns zero matches
  rather than erroring, since RediSearch parses hyphens as query syntax.
  Handled by _escape_tag() and covered by a dedicated regression test.

## [0.2.1] - 2026-08-31
### Fixed
- README's Install section was missing `uv add` variants for both
  extras (only `pip install` was shown) - caught by checking the real
  rendered PyPI project page after the v0.2.0 publish, not caught
  beforehand. Every other install example across this project's docs
  shows both pip and uv; this brings the package's own README (which
  becomes the permanent PyPI page content once published) in line.

## [0.2.0] - 2026-08-31
### Added
- `LanceDBBackend` - second vector backend, via LanceDB's embedded/local
  connection mode (a directory path, no server required). Available via
  the `lancedb` optional extra (`pip install ragleap-vectorstores[lancedb]`).
- Real upsert semantics via `merge_insert()` - re-inserting the same
  document_id/chunk_index updates the row in place rather than
  duplicating it.
- `supports_sparse()` correctly reports `False` for LanceDB - it does
  support full-text search via tantivy, but that surface wasn't
  implemented or tested in this release, so hybrid search honestly
  falls back to dense-only.

## [0.1.0] - 2026-08-29
### Added
- Initial package scaffold - pyproject.toml, package layout matching
  ragleap-graph's src/ragleap_vectorstores convention.
- `ChromaBackend` - first vector backend, implementing the full
  `VectorBackend` interface via chromadb's embedded PersistentClient
  (no server required). Available via the `chroma` optional extra
  (`pip install ragleap-vectorstores[chroma]`).
- `supports_sparse()` correctly reports `False` for Chroma - no native
  keyword/BM25 search as of chromadb 1.5.9, so hybrid search honestly
  falls back to dense-only rather than claiming unimplemented capability.
