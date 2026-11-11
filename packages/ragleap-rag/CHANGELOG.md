# Changelog

All notable changes to `ragleap-rag` are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- Opt-in multi-tenant scope: `RagLeap.for_tenant(tenant_id)` returns a view that tags every ingest with `tenant_id`, adds it to every search filter, prefixes session ids, and lists, deletes and updates only that tenant's documents. `RagLeap(..., require_tenant=True)` makes unscoped calls raise `TenantRequiredError`. Default behaviour is unchanged; documents ingested without a tenant tag are not visible through a view.

## [0.15.0] - 2026-11-06

### Added
- `cheaperinference` is now in the provider base-URL table (`https://api.cheaperinference.com/v1`).
- Evaluation (`ragleap.evaluation`, `RagLeap.evaluate()`): ranking metrics for cases that list `relevant_documents` (hit@k, recall@k, precision@k, reciprocal rank and nDCG@k over the document-level ranking of the chunks sent to the model, read from `answer["citations"]` in chunk order (`answer["sources"]` is built from a set and has no stable order); `metric_ks` picks the cutoffs, default 1, 3, 5, 10), per-case latency and token usage, a `summary` with mean, min, p10, p50, p95 (nearest rank) and a fail rate below `fail_below`, an optional `support_checker(answer_text, citations)` callable for your own NLI model or LLM judge (its failures are recorded per case, not raised), and `compare_runs(baseline, candidate, tolerance)` to flag regressions between two runs. Existing `evaluate()` results keep all their keys; the new keys are additions. This is not an LLM-judge framework and no judge is bundled.

### Changed
- Python 3.10 is no longer supported: `requires-python` is now `>=3.11` and the 3.10 classifier is gone. Pillow 13, which fixes the open Pillow security alerts, needs Python 3.11 or newer; keeping 3.10 kept a vulnerable Pillow 12.3.0 entry in `uv.lock`. Users on Python 3.10 keep working with ragleap-rag 0.14.0 (pip skips newer versions it cannot install). CI already tested this package on Python 3.11 only.

### Fixed
- `answer["sources"]` (from `ask()` and the generation service) now lists each document once, in the order its first chunk was sent to the model. It was built from a set, so the order was different from run to run.

## [0.14.0] - 2026-10-22

### Upgrade note
- Score thresholds: `FAISSBackend` and `PineconeBackend` now return `similarity_score` on the 0 to 1 scale (see Changed). If you filter or alert on those scores, re-check your thresholds.

### Added
- `demonroute` is now in the provider base-URL table (`https://api.demonroute.com/v1`).

### Changed
- Behavior change: `FAISSBackend` and `PineconeBackend` now return `similarity_score` on the same [0, 1] scale as the other backends, `(x + 1) / 2` of the raw cosine (identical 1.0, orthogonal 0.5, opposite 0.0). Before, they returned the raw cosine in [-1, 1], so a caller filtering on `similarity_score` behaved differently per backend (issue 577). Scores of existing stored data are not affected; only the returned values change.

### Fixed
- `ingest_batch()` now keeps the `metadata` of `file` items (it was silently dropped; the url, image, audio and video items already kept it). `aingest()` accepts an optional `metadata` argument for this.

### Verified
- The FAISS score change is tested with a real FAISS index (identical 1.0, orthogonal 0.5, opposite 0.0); the Pinecone change with the mocked client, not against a real Pinecone account.
- The `ingest_batch()` metadata fix has new tests that fail on the previous code.

## [0.13.0] - 2026-10-03
### Security
- `ingest_url()` / `fetch_url_text()` now download pages through a guarded fetcher (`ragleap/_net.py`) instead of `trafilatura.fetch_url`: http/https only, no credentials in the URL, every resolved address must be public (loopback, private, link-local, CGNAT and multicast ranges are refused, including IPv4-mapped IPv6), the connection goes to the validated address so a second DNS lookup cannot swap it, redirects are followed manually (max 3) with every hop re-validated, and size (10 MB) and time are capped. Before this, a page served on a loopback address was fetched. Refusals raise `UnsafeURLError` (a `ValueError`). Pass `allow_private_urls=True` to `ingest_url()` (or `allow_private=True` to `fetch_url_text()`) to reach private networks.
- Zip-based extraction has limits: archives (`.zip`, `.docx`, `.xlsx`, `.pptx`, `.odt`, `.ods`, `.odp`, `.epub`) with more than 1,000 members or more than 100 MB of declared uncompressed data are rejected before extraction with a `ValueError`, and `.zip` members are also read under a running byte budget, A header that understates the sizes did not get around this in a test: Python's `zipfile` reads only the declared size and then fails the CRC check (a 40 MB member with its sizes forged to 1,000 bytes raised `ValueError` with peak memory unchanged), so the budget is a second guard. The forged-header case was not tested for the other zip-based formats. Before this, a 39,002-byte zip returned 40,000,016 characters with no error. The limits are module constants (`ragleap.parsers.MAX_ZIP_MEMBERS`, `MAX_ZIP_UNCOMPRESSED_BYTES`).

### Changed
- Behavior change (hence the minor version): URLs resolving to non-public addresses no longer work unless opted in, archives over the zip limits (including a legitimate `.docx` or `.xlsx` above 100 MB uncompressed) are rejected, and pages are downloaded with Python's `http.client`/`ssl` (system CA certificates; gzip responses are decoded under the same size cap) instead of trafilatura's downloader.

### Verified
- Local run, Python 3.10, all extras installed (formats, web, qdrant, weaviate, pinecone, milvus), Postgres test database, `CI=true`: 368 tests, 354 passed, 14 skipped (12 live-gated Qdrant/Weaviate tests that need running servers, 2 that need `onnxruntime`).
- Compared with the previous downloader (`trafilatura.fetch_url`) on 11 public pages (Wikipedia, python.org about and downloads, docs.python.org, docs.pytest.org, GitHub, ragleap.com, docs.ragleap.com, RFC 9110, Hacker News, example.com over http): extracted text length was identical on all 11. The two python.org pages return gzip even when identity is requested, so the first draft of this fetcher returned nothing for them; that is why gzip decoding was added.
- Real `https://example.com/` and `http://example.com/` fetched through the guard; `http://169.254.169.254/` and `http://127.0.0.1:1/` refused (run before the gzip change, which did not touch the address checks; the attack-case tests cover them in this build).
- A 40 MB zip member with its header sizes forged to 1,000 bytes raised `ValueError` and peak memory stayed flat (102 MB before and after).
- Not verified: Python 3.11 on the test host (CI runs 3.11); forged headers for zip-based formats other than `.zip`; sites that block non-browser User-Agents; br/zstd encodings; pages that need JavaScript.

### Known, not fixed here
- Name resolution (`getaddrinfo`) is not covered by the timeouts. 6to4/NAT64 addresses that embed an IPv4 address are not specially handled. Proxy environment variables are not honored.
- Responses with a `Content-Encoding` other than identity, gzip or zlib-wrapped deflate (for example br) are not decoded and return `None`. python.org sends gzip even when asked for identity, which the first draft of this fetcher refused; gzip is now decoded under the same 10 MB cap, applied to the compressed and the decompressed size.
- Tracked in issue #534.

## [0.12.10] - 2026-10-02
### Fixed
- `pip install "ragleap-rag[web]"` gave a broken `ingest_url()` on a fresh install: `trafilatura` imports `lxml.html.clean`, which is now the separate `lxml_html_clean` package, and nothing installed it. The `web` and `all` extras now include it, and `fetch_url_text()` reports the real import error instead of a generic "requires the 'web' extra" message. (Reproduced on Python 3.10 with lxml 6.1.3 and trafilatura 2.2.0; with `lxml_html_clean` added the import worked and a loopback page extracted correctly.)
- `.parquet` extraction failed with a raw `ModuleNotFoundError` for `pandas`: `_extract_parquet` calls `Table.to_pandas()` and no extra installed pandas. `pandas` is now in the `formats` and `all` extras, and a missing install raises the usual `ValueError` with an install hint. Output is unchanged for existing users.
- `ragleap.__version__` reported `0.12.6` in the published 0.12.9 wheel (package metadata said 0.12.9). It is now `0.12.10`, and a test compares it with `pyproject.toml`.
- The `RagLeap.ingest()` docstring said it handled only .txt/.pdf/.docx.

### Changed
- Tests: an autouse fixture in `tests/conftest.py` removes `*_API_KEY`, `*_EMBEDDING_MODEL`, `*_EMBEDDING_DIMENSIONS`, `*_EMBEDDING_BASE_URL` and `EMBEDDING_DIMENSIONS` for every test. 12 config-validation tests (11 in `tests/test_embedding.py`, plus `tests/test_pinecone_backend.py::test_requires_api_key`) failed whenever those variables were set, because `EmbeddingConfig` and `PineconeBackend` fall back to the environment.
- CI: `ragleap-rag-tests` now installs the formats, web, qdrant, weaviate, pinecone and milvus extras. Before this, 63 mocked Pinecone/Milvus/Qdrant/Weaviate tests were skipped in CI because their client packages were missing (they passed locally with the extras installed). A CI-only guard test fails if an extra goes missing, so tests cannot silently turn into skips again.
- New `tests/test_parsers.py`: one minimal generated sample per supported extension (all 28), plus checks for the legacy-format and missing-pandas errors. `xlwt` was added to the `test` extra to generate the `.xls` sample.

### Verified
- Local run, Python 3.10, all extras installed, Postgres test database, `CI=true` so a missing extra fails instead of skipping: 326 tests, 312 passed, 14 skipped (12 live-gated Qdrant/Weaviate tests that need running servers, 2 that need `onnxruntime`). Without `CI=true`: 293 passed, 33 skipped (the 19 extras-guard tests skip locally).
- Minimum versions installed and exercised: `pandas==2.2.2`, `lxml_html_clean==0.4.0`, `xlwt==1.3.0` (parquet extraction output and the trafilatura import checked).
- CI on this PR's branch (Python 3.11, Postgres service, all extras, run 36963231731): 308 passed, 18 skipped.
- Python 3.11.15 with pandas 3.0.6 and pyarrow 25.0.1 (separate venv): parquet extraction of a table containing a null and floats gave the same output as with pandas 2.2.2 (`'a\tb\nx\t1.5\n\t2.0\n'`); `test_parsers`, `test_web_import_error`, `test_package_metadata` and `test_embedding`: 63 passed, 1 failed - `test_ollama_full_rag_integration_with_faiss`, because that venv was created without the faiss extra (it passes in the full-extras runs).
- Not verified: the `pyarrow>=14.0.0` floor. The 28 per-extension tests use minimal generated samples, not complex real-world documents.

### Known, not fixed here
- `_extract_zip` has no limit on member count or expanded size (a 39,002-byte zip returned 40,000,016 characters with no error).
- `fetch_url_text()` / `ingest_url()` do not check the target address (a loopback URL was fetched), and failures return `None`, so "blocked or too large" looks like an empty page.
- Tracked in issue #534.

## [0.12.9] - 2026-09-26
### Fixed
- REAL BUG in WeaviateBackend, live-verified against a real Weaviate
  instance: `init_schema()`'s else branch called
  `weaviate.connect_to_local()` with no arguments. That function's real
  installed signature (weaviate-client==4.22.0/4.23.1) hardcodes
  `host="localhost"`, `port=8080`, `grpc_port=50051` with no override
  through this backend's own constructor - so any self-hosted Weaviate
  not on the exact default port was unreachable (Weaviate Cloud via
  `cluster_url=` was unaffected). Fixed by adding optional
  `local_host=`/`local_port=`/`local_grpc_port=` constructor
  parameters, defaulting to `connect_to_local()`'s own defaults so
  existing behavior is unchanged when unset.
- REAL BUG in WeaviateBackend, found via the same live test:
  `Configure.Vectors.self_provided()` with no `name=` creates a NAMED
  vector called `"default"` (confirmed via the real collection config:
  `vector_config={'default': _NamedVectorConfig(...)}`), not the legacy
  unnamed vector space. `insert_chunk()` passed a plain list to
  `data.insert(vector=...)` and `search_dense()` called
  `near_vector()` with no `target_vector=` - empirically confirmed via
  three live test cases that BOTH sides needed to change together
  (neither alone fixed it): `search_dense()` silently returned zero
  results for every query. Fixed by using `vector={"default": [...]}`
  on insert and `target_vector="default"` on query.
- REAL BUG in WeaviateBackend, found while fixing the above: the
  existing `1.0 - distance` distance-to-similarity conversion produced
  raw cosine similarity in `[-1, 1]` (live-verified: identical/
  orthogonal/opposite vectors scored 1.0/0.0/-1.0), not `pgvector`'s
  `[0, 1]` convention (`1 - distance/2`) - the same class of bug
  already found and fixed for `MilvusBackend` and `QdrantBackend`
  (this release). Fixed to match `pgvector`'s exact formula
  (`1.0 - distance / 2`).

### Changed
- `WeaviateBackend` is now genuinely live-verified: a real smoke test
  against a real Weaviate instance (init_schema, insert_document,
  insert_chunk, search_dense with and without metadata_filter,
  list_documents, get_document_filename, delete_document) passed after
  all three fixes above. The module docstring's "NOT LIVE-VERIFIED"
  claim is corrected.
- New `tests/test_weaviate_backend_live.py`: always-available
  live-gated tests (skip cleanly when `WEAVIATE_TEST_URL` is unset),
  same pattern as the OpenSearch/Upstash/Qdrant live tests, so this
  class of bug (mock-only tests encoding wrong assumptions) can't
  regress silently again.

### Not yet verified
- MilvusBackend and PineconeBackend remain code-complete but not
  live-verified (no Milvus or Pinecone instance tested against). See
  the tracking issue.

## [0.12.8] - 2026-09-24

### Fixed

- `ingest()` now accepts an optional `metadata: Optional[Dict] = None`
  parameter, threaded through to `ingest_text()`. This was a real,
  unexplained inconsistency: `ingest_url()` and `ingest_image()` -
  `ingest()`'s two siblings for raw-input ingestion - both already had
  this parameter. `ingest()` silently dropped the ability to attach
  metadata at ingest time. Discovered downstream while fixing the same
  gap in `ragleap-tools`' `ingest_document` tool (v0.1.1) - any caller
  using `ingest()` directly, arguably the most common entry point since
  it's the one that does format auto-detection, hit the identical
  problem. Backward compatible: defaults to `None`, no existing caller
  (including `ragleap-graph`) is affected. See
  `docs/design/ingest-metadata-parameter.md` for the full writeup,
  including a note for `ragleap-agents`/`ragleap-integrations`/
  `ragleap-flows` on the (currently unenforced) `{"filename": ...}`
  metadata-key convention this enables.

### Verified

- 2 new live tests against real Postgres in `tests/test_ingestion.py`:
  metadata is actually stored and retrievable via `ingest()` (not just
  "call succeeds"), and the default no-`metadata=` path is unchanged.
  Full suite: 255 passed, 10 skipped (unchanged skip count - no live
  credentials needed for this fix).

## [0.12.7] - 2026-09-23
### Fixed
- REAL BUG in QdrantBackend, live-verified against a real Qdrant
  instance (identical vectors scored 1.0, orthogonal 0.0, opposite
  -1.0): `search_dense()` returned Qdrant's raw `Distance.COSINE`
  score directly as `similarity_score`, which is raw cosine similarity
  in `[-1, 1]` - unlike `pgvector`/`weaviate_backend.py`/
  `milvus_backend.py`, which all normalize to `[0, 1]`. A caller
  filtering `similarity_score > 0.5` behaved correctly for those three
  backends and silently misbehaved for Qdrant's negative scores - the
  same inconsistency already found and fixed once in this codebase for
  `MilvusBackend` (v0.11.0), previously missed here. Fixed with the
  identical `(x + 1) / 2` transform.

### Changed
- `QdrantBackend` is now genuinely live-verified: a real smoke test
  against a real Qdrant instance (init_schema, insert_document,
  insert_chunk, search_dense with and without metadata_filter,
  list_documents, get_document_filename, delete_document) passed after
  the score-normalization fix. The module docstring's
  "NOT LIVE-VERIFIED" claim is corrected.
- New `tests/test_qdrant_backend_live.py`: always-available live-gated
  tests (skip cleanly when `QDRANT_TEST_URL` is unset), same pattern as
  the OpenSearch/Upstash live tests in `ragleap-vectorstores` - so this
  class of bug (mock-only tests encoding a wrong assumption) can't
  regress silently again.

### Not yet verified
- WeaviateBackend, MilvusBackend, and PineconeBackend remain
  code-complete but not live-verified (no Weaviate/Milvus/Pinecone
  instance tested against in this release). See the tracking issue.

## [0.12.6] - 2026-09-13

### Fixed

- `EmbeddingConfig(provider="gemini", dimensions=...)` silently ignored `dimensions=` - `_embed_gemini()` and `_embed_batch_gemini()` never passed it to the real `google.genai` client call at all, despite `dimensions=` being validated as *required* at config-construction time (`__post_init__` raises `ValueError` if unset). Every other provider path that supports a configurable dimension (`openai`) correctly passed it through; Gemini specifically did not. Concretely: requesting `dimensions=768` against `gemini-embedding-001` silently returned the model's full 3072-dimension default instead, which would fail loudly downstream (a `psycopg2.errors.DataException: expected N dimensions, not 3072` against a pgvector column sized for the requested dimension) rather than fail at the point of the actual mistake.
- Fix: both `_embed_gemini()` and `_embed_batch_gemini()` now pass `config=google.genai.types.EmbedContentConfig(output_dimensionality=self.config.dimensions)` to the real API call, matching the documented `google-genai` SDK parameter for controlling output embedding size.
- Found while live-testing the #153 eval framework tooling (`ragleap-rag` + `ragleap-graph` comparison) against a real Gemini embedding call - a genuine live run surfaced this, not a code read.

### Verified

- Full suite: 252 passed, zero regressions.
- Live-verified against the real `google.genai` API: a real embed call requesting `dimensions=768` now actually returns a 768-length vector (confirmed by successful insertion into a pgvector column sized to 768, which previously failed with the dimension mismatch above).

## [0.12.5] - 2026-09-13

### Fixed

- README's `<!-- AUTO-STATS -->` badge was stuck at "v0.12.2 · 244 tests" - three releases stale (real state: v0.12.4, 252 tests at time of writing). Found via a documentation-claims audit; the `AUTO-STATS` markers imply automation that doesn't actually exist (confirmed - no script in this repo regenerates them), so this drifted silently across multiple releases. No code changes in this release; it exists specifically so the corrected README reaches PyPI, which only refreshes its displayed README on a new upload, not from a live GitHub pull.
- Re-verified (not just carried over) the three headline capability claims in the README - 6 vector backends, 8 embedding providers, 12+ generation providers - against real source counts. All still exactly accurate.

## [0.12.4] - 2026-08-27

### Fixed

- `chunk_text()`'s `token_count` field now reflects a real LLM token count via `tiktoken` (`cl100k_base`) when available, instead of always being a whitespace-split word count. The previous behavior was a documented, known limitation (`token_count` internally consistent but not what the field name implied). `_tokenize()` itself — which determines chunk windowing (`chunk_size`/`chunk_overlap` are still measured in whitespace-split words) — is unchanged; only what `token_count` reports about the resulting chunk text has changed. A new `token_count_is_exact: bool` field is added alongside the existing `token_count` field: `True` when a real tiktoken count was used, `False` when tiktoken wasn't installed or its encoding couldn't be loaded (e.g. no network egress to fetch the BPE rank file on first use), in which case `token_count` falls back to the previous word-count behavior exactly, never reporting an estimate as exact. New tests (`test_chunk_text_token_count_matches_tiktoken_when_exact`, `test_chunk_text_fallback_word_count_when_tiktoken_unavailable`, `test_chunk_text_fallback_on_encoding_load_failure`) cover both the real-tokenization and fallback paths. `tiktoken` is now a declared core dependency rather than an incidental transitive one. This is a breaking change to `token_count`'s *values* for any caller depending on the previous word-count numbers specifically — acceptable pre-1.0, and this was already a documented, known limitation rather than a silent surprise.

## [0.12.3] - 2026-08-23

### Fixed

- Milvus backend's `similarity_score` is now normalized to `[0, 1]`, matching `pgvector`/`weaviate`'s convention. Milvus with `metric_type="COSINE"` returns raw cosine similarity in the field it labels "distance" - verified against Milvus's own documentation, this was already correctly ordered (higher = more similar), not backwards, but its range was inconsistent: raw cosine similarity spans `[-1, 1]`, while `pgvector` (`1 - distance/2`) and `weaviate_backend.py` (`1.0 - distance`) already normalize to `[0, 1]`. A caller filtering on `similarity_score` (e.g. `score > 0.5`) would behave correctly for those two backends but break silently for Milvus's negative scores. Fixed via the standard `(x + 1) / 2` linear transform - preserves exact ranking order, adds consistent range. New regression test (`test_search_dense_normalizes_cosine_similarity_to_unit_range`) confirms the transform at all three boundaries (identical/orthogonal/opposite vectors -> 1.0/0.5/0.0). This is a breaking change to output values for anyone depending on Milvus's previous raw `[-1, 1]` scores - acceptable pre-1.0, and this inconsistency was already a documented, known limitation rather than a silent surprise.

## [0.12.1] - 2026-08-08

### Fixed
- PyPI metadata (description, keywords) corrected. The previous draft description claimed "knowledge-graph integration via ragleap-graph" - backwards: ragleap-rag has zero dependency on or awareness of ragleap-graph; it is the other way around (ragleap-graph optionally depends on ragleap-rag, per retrieval.py's own docstring: "No knowledge-graph coupling here by design"). Corrected to describe ragleap-rag's real, standalone feature set accurately, with ragleap-graph mentioned only as an optional composable package - not an integration ragleap-rag itself has. Also removed the "neo4j" keyword, which implied a dependency that does not exist.

## [0.12.0] - 2026-08-08

### Added
- `RagLeap.retrieve(query, top_k=5, hybrid=True, rerank=False, metadata_filter=None)` - retrieves chunks using the same embed -> search -> (optional rerank) pipeline as `ask()`, stopping before generation. Returns raw chunks, not an answer.
- Added to give external callers a real, public, tested entry point to retrieval-only results, instead of needing an LLM generation call they don't want, or reaching into private internals (`_vector_backend`, `_embed_query_cached()`) across a package boundary. First concrete consumer: `ragleap-graph`'s upcoming `GraphRetriever` (v0.3.0), which combines this with graph traversal.
- Does not support `query_rewrite=` or `session_id=` - those involve LLM calls and conversation-history orchestration that `ask()` owns; pass an already-rewritten query string directly if needed.
- 6 new tests covering top_k, metadata_filter, hybrid/dense-only mode, rerank plumbing, empty-corpus behavior, and (critically) that `retrieve()` never triggers generation - verified via a test that makes the generator explode if called, so a future regression back to invoking `generate_answer()` fails loudly rather than just costing unnoticed extra tokens.

## [0.11.2] - 2026-08-04

### Fixed
- The README's "How it fits together" diagram fix in v0.11.1 was incomplete: the ingestion box still only listed `.txt/.pdf/.docx`, omitting the real 28-format + URL + image + audio + video ingestion support that already exists (`ingest()`, `ingest_url()`, `ingest_image()`, `ingest_audio()`, `ingest_video()`). Corrected to reflect actual ingestion breadth.
- The separate Celery background-task architecture diagram showed "PostgreSQL + pgvector" as if it were required for background/async usage. Clarified that this reflects only the example's default (it omits `vector_backend=`) — any of the 6 supported backends works identically in the same Celery pattern.

### Documentation
- Celery integration guide + runnable example ([#57](https://github.com/antonyrag/ragleap-core/pull/57))

### Testing
- Comprehensive pytest suite (67 tests) + CI integration — first automated testing this package has had ([#58](https://github.com/antonyrag/ragleap-core/pull/58))

## [0.11.1]

### Fixed
- README's "How it fits together" architecture diagram was stale - hardcoded "PostgreSQL + pgvector" as the only storage layer (predates vector backend pluggability), and showed nothing about query rewriting, structured output, or cost tracking, all of which now sit in the actual request flow. Updated to reflect the real current architecture.

## [0.11.0]

### Added

- Three new vector backends completing the originally-planned roadmap: `WeaviateBackend`, `QdrantBackend`, `MilvusBackend` - all implementing the `VectorBackend` interface, following the same SQLite-sidecar-plus-remote-vector-store pattern as `PineconeBackend`.
- New `[weaviate]`, `[qdrant]`, `[milvus]` extras.
- **Not live-verified** against real instances of any of the three (no accounts were available) - same honest caveat as `PineconeBackend`. Every method signature, class field, and return-type structure used in all three implementations was introspected directly against each package's actual installed source code (`weaviate-client==4.22.0`, `qdrant-client==1.18.0`, `pymilvus==3.0.1`) during development - not assumed from documentation. This is the same rigor that caught a real pre-ship bug in `PineconeBackend` (v0.10.1); applying it up front here rather than discovering issues later.
- `WeaviateBackend`: vectors are "self-provided" (`Configure.Vectors.self_provided()`) since ragleap-rag always brings its own embeddings. Weaviate assigns its own UUIDs on insert (can't supply an arbitrary string ID directly like Pinecone), so a deterministic UUID derived from `document_id:chunk_index` is used - re-running the same insert stays idempotent. Distance is converted to a similarity-style score (`1.0 - distance`) for consistency with the other backends, which all return higher-is-better values.
- `QdrantBackend`: `PointStruct.id`'s type hint technically allows arbitrary strings, but real runtime validation requires string IDs to actually parse as valid UUIDs (a well-documented gap between the type system and the server's actual behavior) - deterministic UUIDs used here too, for the same reason as Weaviate.
- `MilvusBackend`: unlike the other three, Milvus's `id_type="string"` genuinely accepts arbitrary string primary keys directly - no UUID workaround needed, `document_id:chunk_index` is used as-is. `enable_dynamic_field=True` (the client's own default) means the caller-supplied metadata dict can be inserted directly as extra fields without pre-declaring a schema for them.
- All three, like `PineconeBackend`, require `persist_directory=` (not optional like `FAISSBackend`) - a remote/cloud vector store persists data regardless of local process state, so a persistent SQLite sidecar for chunk text is mandatory to avoid orphaned vectors after a restart.
- `supports_sparse()` is `False` for all three, even though Weaviate/Qdrant/Milvus all natively support sparse vectors/BM25/hybrid search - implementing that natively for each is a real, valuable future enhancement, deliberately not attempted here to keep initial scope honest and verifiable rather than guessing at a larger untested surface area.
- 40 new tests across three files (198 -> 238 passing): each backend gets constructor validation, pure helper-function tests, SQLite CRUD in isolation, and mocked-client interaction tests confirming the right SDK methods get called with the right arguments and attribute-access patterns.
- This completes the originally-planned vector backend roadmap (Pinecone, Weaviate, Qdrant, Milvus) - `ragleap-rag` now supports 6 vector storage options total: pgvector, FAISS, Pinecone, Weaviate, Qdrant, Milvus.

## [0.10.1]

### Added

- `PineconeBackend` - a real, working implementation of the `VectorBackend` interface for Pinecone's managed serverless vector database, following the same SQLite-sidecar pattern as `FAISSBackend`. New `[pinecone]` extra.
- Every attribute path used (`IndexList.names`, `IndexStatus.ready`, `ScoredVector.id`/`.score`, `Pinecone.Index(host=...)`) was verified directly against the actual installed `pinecone==9.1.0` package's real source code during development - not assumed from documentation or memory. This caught a real bug before it shipped: an early draft used dict-style access (`idx["name"]`, `status.get("ready")`) that would have failed at runtime, since the SDK's response objects are `msgspec.Struct`s requiring attribute access throughout, not dicts.
- **Not live-verified** against a real Pinecone account (none was available) - 20 tests cover constructor validation, pure helper functions, SQLite CRUD in isolation, and mocked-client interaction tests (confirming the right SDK methods get called with the right arguments), but none of this is a substitute for a real end-to-end run against Pinecone's actual service.
- Deliberate design choice: `persist_directory=` is **required**, not optional like `FAISSBackend`'s - Pinecone vectors persist remotely regardless of local process state, so an in-memory-only SQLite sidecar would create orphaned vectors (searchable in Pinecone, with no matching text) the instant the process restarts.
- `metadata_filter` uses Pinecone's real native filtering (translated to its `$eq` syntax), not a post-filter like `FAISSBackend` has to do - the caller-supplied metadata dict is stored in Pinecone's own vector metadata, not just SQLite.
- Serverless index creation readiness is polled with a 60s timeout - a well-documented Pinecone requirement (indexes aren't instantly queryable after creation) that's easy to miss.

### Corrected

- Prior CHANGELOG entries (v0.7.0) and README text referenced "the same caveat already attached to the Pinecone vector backend" - **this was inaccurate**. No Pinecone backend existed in the codebase at that time; the phrase was carried over from an earlier, unverified project-planning document (the original session handoff) that had incorrectly described Pinecone as "code-complete but unverified" when it had, in fact, never been built. Historical CHANGELOG entries are left as-written (a changelog is a historical record, not silently rewritten after the fact), but README.md - being living documentation, not a historical record - has been corrected to reflect that `PineconeBackend` now genuinely exists as of this release.

## [0.10.0]

### Added

- Query rewriting/expansion: `query_rewrite=` on `ask()` with three strategies grounded in established RAG research - `"contextual"` (resolves follow-up questions using session history, HyDE-adjacent single-call pattern cited in 2026 production case studies), `"hyde"` (Gao et al. 2022 - embeds a generated hypothetical answer instead of the raw query), and `"multi_query"` (Rackauckas 2023 RAG-Fusion - generates alternative phrasings, retrieves for each, merges via Reciprocal Rank Fusion).
- New `ragleap.query_rewrite` module: `contextual_rewrite()`, `hyde_document()`, `multi_query_variants()`, `reciprocal_rank_fusion()`.
- **Live-verified this release** with real Gemini calls: contextual rewrite correctly resolved a pronoun-laden follow-up ("it" -> "RagLeap"), HyDE generated an on-topic hypothetical passage, multi_query produced three genuinely distinct phrasings that all correctly retrieved the same chunk, RRF merge correctly ranked the doubly-retrieved chunk first. Full integration verified end-to-end (real embeddings, real FAISS retrieval, real generation) for all three strategies plus the no-rewrite backward-compat path.
- Honest limitation documented, not hidden: `multi_query`'s generated variants can be "nearly identical and lacking in diversity" (a documented finding in the broader RAG-Fusion literature) and it costs `multi_query_n` retrieval calls instead of one - `contextual` and `hyde` are the cheap, fast options if that's what you want.
- Every strategy fails open: if the extra rewrite LLM call itself fails, retrieval proceeds with the original, unmodified query - a broken rewrite step can never break retrieval entirely.
- The final answer generation always uses the original query, never the rewritten form - rewriting only affects what gets retrieved.
- The rewrite call's real token cost is recorded into the existing `CostTracker` infrastructure, contributing to `cumulative_cost_usd`.
- `multi_query_n=3` param on `ask()` controls variant count for the `multi_query` strategy.
- Result gains a `query_rewrite` field (strategy + rewritten_query/hyde_document/query_variants) only when `query_rewrite=` is passed - absent entirely otherwise, for backward compatibility.
- Not supported on `ask_stream()`.
- 19 new tests (159 -> 178 passing): isolated unit tests for `ragleap.query_rewrite` (via a lightweight stub generator for deterministic control), fail-open behavior coverage, and `ask()` plumbing tests via the existing fake-provider fixtures.

## [0.9.0]

### Breaking

- **No provider ever hardcodes a default model again** - `ProviderConfig` (Gemini, Anthropic) and `EmbeddingConfig` (all 7 embedding providers) now require `model=` explicitly (constructor arg or env var), and `EmbeddingConfig` additionally requires `dimensions=` explicitly for every provider - not just `together`, which set this precedent from the start.
- This is a deliberate reliability-over-convenience decision, not an oversight: v0.8.1 fixed a hardcoded Gemini default that broke in production when Google deprecated it. Investigating a "safer" fix (using Google's own `gemini-flash-latest` rolling alias instead of a pinned model string) found that the alias mechanism itself has also been deprecated before - there's no model string, pinned or aliased, that stays permanently safe to hardcode in a fast-moving space. Rather than trade one staleness risk for another, the library now always asks you to know and specify your model.
- **Migration**: any code relying on an implicit default (e.g. `ProviderConfig(provider="gemini", api_key="...")` with no `model=`, or `EmbeddingConfig(provider="gemini", api_key="...")` with no `model=`/`dimensions=`) will now raise a `ValueError` with a clear message telling you exactly what to pass, or which environment variable to set instead.
- Every example in `examples/`, the README, and the module docstring updated accordingly.

### Added

- `provider="custom"` + `base_url=...` support for `EmbeddingConfig`, mirroring `ProviderConfig`'s existing support for chat models - reaches any OpenAI-compatible embeddings endpoint not otherwise named (self-hosted servers, or providers without dedicated code here yet). Several Chinese providers - Qwen/DashScope, Zhipu/GLM, Moonshot/Kimi - ship OpenAI-compatible modes and work via this path today, alongside the already-named `deepseek` in `ProviderConfig` and aggregators like `openrouter`.
- 9 new tests (150 -> 159 passing): `test_embedding.py` rewritten to test the new explicit-required contract instead of old default-resolution behavior, plus 4 new tests covering the `custom` embedding provider.

## [0.8.1]

### Fixed
- Default Gemini generation model changed from `gemini-2.5-flash` (deprecated by Google, returns `404 NOT_FOUND` on new API calls as of this release) to `gemini-3.6-flash` - the current GA general-purpose Flash tier, confirmed via live web search (BenchLM pricing sync 2026-07-28, innFactory release notes 2026-07-21) and matching the model already used in `cost.py`'s seed pricing table. Affects only callers relying on the default `ProviderConfig(provider="gemini", ...)` without passing `model=` explicitly. Discovered during v0.8.0's live structured-output verification (see that release's "Known issue" note).
- `gemini-2.5-flash-lite` (a different, still-current cheaper tier per the same search) is unaffected and unchanged.
- Two `test_cost.py` tests were coupled to the old default model string and started failing once it changed (one because the new default is now priced, one because it hardcoded the old model name in a pricing_table override) - both fixed by using explicit `model=` rather than relying on whatever the library's default happens to be, since that default has now demonstrably changed once already.

## [0.8.0]

### Added
- Structured/JSON output mode: `response_format=<JSON schema dict>` on `ask()`. Result gains `structured` (parsed object or `None`), `structured_valid` (bool), `structured_enforcement` (`"native"` or `"json_object_fallback"`), `structured_validation_method` (`"jsonschema"` or `"basic_type_check_only"`).
- New `ragleap.structured` module (`parse_and_validate()`, `parse_and_validate_object()`), new `[structured]` extra (`jsonschema>=4.0.0`) for real schema validation - without it, only a basic top-level type check runs, honestly flagged as such rather than claiming full validation.
- Gemini uses native `response_schema` constrained decoding. Anthropic uses forced tool-use (no OpenAI-style `response_format` exists for it) - a single tool whose `input_schema` is the desired shape, reading the parsed tool input directly. Both report `"native"` enforcement.
- OpenAI-compatible providers (OpenAI, Mistral, Together, Ollama) try strict `json_schema` mode first, honestly falling back to unconstrained `json_object` mode on failure (`"json_object_fallback"`) rather than silently claiming equal guarantees.
- **Live-verified this release** with a real Gemini API call (not mocked): correctly extracted structured city/country/population data from source context, passed real `jsonschema` validation. Anthropic/OpenAI-compatible paths are code-complete per each provider's public documentation but not live-verified against a real account this session.
- Not supported on `ask_stream()` - structured output needs the complete response before it's valid JSON; documented as a real gap, not silently unsupported.
- `_call_provider()`'s internal return contract changed from a 2-tuple to a 3-tuple (added enforcement mode) - a private-method change, not part of the public API, but noted for anyone who monkeypatched it directly (e.g. in tests).
- 11 new tests (139 -> 150 passing): `ragleap.structured` unit tests including a forced no-jsonschema-installed fallback path, plus `ask()` plumbing tests proving `response_format=` flows correctly end-to-end and real validation runs (not rubber-stamped).

### Known issue (not fixed this release)
- Discovered during live verification: the library's default Gemini model, `gemini-2.5-flash`, is now deprecated by Google (`404 NOT_FOUND` on new API calls) - affects any caller relying on the default rather than passing `model=` explicitly. Flagged as a follow-up, out of scope for this release.

## [0.7.0]

### Added
- Embedding provider expansion: Mistral, Together, Ollama, Cohere, and Voyage AI, alongside existing Gemini/OpenAI support.
- Mistral, Together, and Ollama reuse the existing `openai` package's client pointed at a custom `base_url` (all three expose OpenAI-compatible `/embeddings` endpoints) - no new dependency.
- Cohere and Voyage AI use `requests` (already a core dependency) directly, since their response shapes differ from OpenAI's.
- `ollama` is live-verified this release: fully local, no API key required, tested end-to-end (`embed_text()`, `embed_batch()`, and a full real ingest -> real FAISS retrieval integration test) via `nomic-embed-text`.
- `mistral`, `together`, `cohere`, and `voyage` are code-complete based on public API documentation but NOT live-verified against a real account - same caveat already attached to the Pinecone vector backend. Their default models/dimensions are best-effort until confirmed live.
- `together` requires explicit `model=` and `dimensions=` - no safe default exists across its many incompatible embedding models. Raises a clear error rather than guessing.
- Honest limitation documented for `cohere`: always uses `input_type="search_document"` since `embed_text()`/`embed_batch()` don't currently distinguish query vs. document embedding calls.
- 16 new tests: live Ollama tests (skipped automatically if Ollama isn't running, so CI never fails on its absence) plus config-resolution/error-handling tests for the four unverified providers (123 -> 139 passing).

## [0.6.4]

### Added
- Cost-First RAG: `ask()` now returns a `cost` field (`cost_usd`, `cumulative_cost_usd`, `pricing_available`) computed from real provider-reported token usage. New `ragleap.cost` module (`compute_cost()`, `CostTracker`).
- Built-in seed pricing table for Gemini, Anthropic, and OpenAI, verified via live web search on 2026-07-28 (not from training-data memory, since pricing changes fast). `pricing_table=` on `RagLeap.__init__()` merges with/overrides the seed table — user-supplied entries always win. Unknown provider/model combinations return `cost_usd: None`, never a guessed number.
- Budget-triggered fallback: `budget_usd_per_month=` + `budget_fallback=` on `RagLeap.__init__()`. Once cumulative spend crosses the budget, subsequent `ask()`/`ask_stream()` calls use `budget_fallback` for that call only via a new non-mutating `override_provider=` param on `GenerationService.generate_answer()`/`generate_answer_stream()` — the shared instance's own primary/fallback chain is never touched, so concurrent calls aren't affected by one request's fallback decision.
- `generate_answer()`'s return dict gained a `model_used` field (alongside the existing `provider_used`) - needed to look up pricing, and a genuinely useful addition on its own.
- `ask_stream()` never reports a real `cost_usd` (streaming has no token usage data - see the Streaming section), but `override_provider` still applies for budget-triggered fallback if configured.
- 17 new tests, including a real end-to-end proof that budget-triggered fallback switches `provider_used` on the second call once the first call's real cost crosses the budget.

## [0.6.3]

### Added
- Observability hooks: `on_ingest=`/`on_query=`/`on_answer=` on `RagLeap.__init__()` - lightweight, fire-and-forget event emission, not a dashboard or storage layer. Each is a list of `(event: dict) -> None` callables.
- `on_ingest` fires after successful ingestion with document_id/filename/chunks_stored/chunks_attempted.
- `on_query` fires after retrieval (before generation) with query/hybrid/rerank/top_k/chunks_retrieved/streaming.
- `on_answer` fires after generation on both `ask()` (provider_used/usage/chunks_sent/guardrail_blocked) and `ask_stream()` (answer_length, since usage isn't available for streaming) - every event includes `streaming: bool`.
- A hook that raises is caught, logged as a warning, and swallowed - never breaks the actual ingest/ask operation. New `ragleap.observability` module (`fire_event()`).
- 7 new tests, including explicit coverage that a broken hook does not break ingestion or asking.

## [0.6.2]

### Added
- `rag.evaluate(test_cases)` - a lightweight, deterministic evaluation utility. Reports retrieval hit rate, keyword coverage, and citation groundedness against a labeled test set. Explicitly NOT an LLM-as-judge framework (no faithfulness/relevancy scoring like Ragas) - that requires careful judge-prompt calibration and is planned as a separate, dedicated tool. This is a fast, free, repeatable sanity check for catching regressions in your own retrieval/generation setup.
- New `ragleap.evaluation` module: `evaluate()` and `evaluate_case()`.
- 8 new tests, including using the fake test generator's real prompt-echo behavior to create genuinely checkable keyword overlap (not coincidental passes).

## [0.6.1]

### Added
- Guardrail hooks: `input_guardrails=`/`output_guardrails=` on `RagLeap.__init__()` - user-supplied validation callbacks that extend (not replace) the existing sanitization/injection-risk detection. Each guardrail is `(text: str) -> str`, or raises `GuardrailViolation` to reject content.
- `input_guardrails` run during `ingest_text()`, after sanitization - a violation aborts ingestion with nothing stored.
- `output_guardrails` run on `ask()`'s answer - a violation replaces the answer with a refusal message and sets `answer["guardrail_blocked"] = True`.
- `ask_stream()` support with an honestly-documented limitation: output guardrails can only run after the full answer is assembled, but tokens are already yielded to the caller by then - a violation during streaming is logged as a warning, not enforced. Use `ask()` instead if blocking bad output before the user sees it matters.
- 8 new tests covering modification, rejection, ordering, and the ask()/ask_stream() enforcement difference.

## [0.6.0]

### Added
- Pluggable vector storage backends via `vector_backend=` on `RagLeap.__init__()`. Vector storage is now decoupled from conversation memory (which always uses Postgres regardless of vector backend choice, since memory is a separate concern).
- `ragleap.vectorstores.VectorBackend` — the abstract interface any backend implements (`init_schema`, `insert_document`, `insert_chunk`, `search_dense`, `list_documents`, `delete_document`, `get_document_filename`, optionally `search_sparse`/`search_hybrid`/`supports_sparse`).
- `PgVectorBackend` — the default, a refactor (not a rewrite) of the existing proven Postgres/pgvector logic. Zero behavior change for existing users; verified via the full existing test suite passing unmodified (73/73) after the refactor.
- `FAISSBackend` — new, local, in-process, no API key required. Requires the `[faiss]` extra. No native full-text search (`supports_sparse()` returns `False`; `ask(hybrid=True)` gracefully degrades to dense-only). Metadata filtering is a post-filter over a SQLite sidecar, not an indexed query. Optional `persist_directory=` for data that survives process restarts — verified live across separate backend instances pointed at the same directory, not just in-process. 10 new tests, all live against a real FAISS index (no mocking of the backend itself).

### Changed
- Internal: `RagLeap._retriever` (the old `VectorRetrievalService`) removed in favor of `RagLeap._vector_backend`. This is a private-attribute rename — anyone reaching into `rag._retriever` directly (not part of the public API) needs to update to `rag._vector_backend` with its new method names (`search_dense`/`search_sparse`/`search_hybrid` instead of `search_similar_chunks`/`search_sparse_chunks`/`search_hybrid_chunks`).
- `schema.py` split into independent core schema (documents/chunks, backend-specific) and memory schema (conversations/conversation_messages, always Postgres) - `init_schema()`/`get_schema_sql()` remain as backward-compatible combined wrappers for anyone calling the module directly.

## [0.5.8]

### Changed
- Reranking now runs on ONNX Runtime (CPU only) instead of torch/sentence-transformers — the `[rerank]` extra no longer pulls in a 2GB+ torch+CUDA install for what is, at this model size, pure CPU inference. A quantized cross-encoder (~23MB, `Xenova/ms-marco-MiniLM-L-6-v2`) downloads once via `huggingface_hub` and is cached locally.

### Breaking (opt-in extra only)
- `RerankerService.__init__()`'s `model_name=` param is now `model_repo=` (a Hugging Face repo id, not a sentence-transformers model name). Only affects callers who passed a custom reranking model explicitly — the default behavior (no argument) is unaffected.

## [0.5.7]

### Fixed
- `ask_stream()` (and `aask_stream()`) were missing `rerank=` and `metadata_filter=` params that `ask()` already had — a real, previously-flagged API inconsistency. Both are now supported identically across the sync/async and streaming/non-streaming variants. Regression-covered by 4 new tests.

## [0.5.6] - 2026-07

### Added
- Redis-backed distributed query embedding cache (`cache_backend="redis"`) — survives process restarts and is shared across multiple worker/Celery processes, unlike the in-memory cache. Requires the `[redis]` extra.
- `ingest_batch()` — concurrent mixed-type ingestion (file/url/image/audio/video in one call) with per-item partial-success results.
- Missing async ingest wrappers: `aingest_url`, `aingest_image`, `aingest_audio`, `aingest_video` — all 6 ingest methods now have async twins.

([#56](https://github.com/antonyrag/ragleap-core/pull/56))

## [0.5.5] - 2026-07

### Added
- Custom transcription provider support (`provider="custom"` + `transcribe_fn`) — bring your own transcription function instead of Whisper/Deepgram.

([#55](https://github.com/antonyrag/ragleap-core/pull/55))

## [0.5.4] - 2026-07

### Added
- Video ingestion (`ingest_video`) — extracts audio via ffmpeg, then transcribes it. Requires the ffmpeg binary on the system.

([#54](https://github.com/antonyrag/ragleap-core/pull/54))

## [0.5.3] - 2026-07

### Added
- Audio ingestion (`ingest_audio`) — pluggable transcription (Whisper or Deepgram).

([#53](https://github.com/antonyrag/ragleap-core/pull/53))

## [0.5.2] - 2026-07

### Added
- Image ingestion (`ingest_image`) — two modes: OCR (Tesseract, requires the `[ocr]` extra + system binary) and vision captioning (Gemini).

([#52](https://github.com/antonyrag/ragleap-core/pull/52))

## [0.5.1] - 2026-07

### Added
- Expanded document format support — 28 formats total (pptx, xlsx, xls, csv, tsv, json, yaml, xml, rtf, odt, ods, odp, html, zip, eml, epub, parquet, vtt, srt, and more). Requires the `[formats]` extra for the non-core formats.

### Fixed
- `__version__` in `src/ragleap/__init__.py` had not been bumped to match `pyproject.toml`'s `0.5.1` — caught via routine `git status` before publish, fixed same-day. No mismatched version reached PyPI.

([#50](https://github.com/antonyrag/ragleap-core/pull/50), [#51](https://github.com/antonyrag/ragleap-core/pull/51))

## [0.5.0] - 2026-07

### Added
- URL ingestion (`ingest_url`) — fetches a page and extracts clean readable text via trafilatura, stripping nav/ads/footers. Requires the `[web]` extra.

([#49](https://github.com/antonyrag/ragleap-core/pull/49))

## [0.4.7]

### Added
- Structured, chunk-level citations (`citations` field on `ask()`'s return value) — resolves ambiguity in `[Source N]` prompt labels: they always refer to a specific chunk, never a whole document.

([#48](https://github.com/antonyrag/ragleap-core/pull/48))

## [0.4.6]

### Added
- Content sanitization (`sanitize_text`) — strips null bytes, control characters, and invisible/zero-width Unicode before chunking. On by default (`sanitize=True`).
- Heuristic prompt-injection risk detection (`detect_injection_risk`) — logs a warning at ingest time on common trigger phrases. Pattern-based, not a guarantee; nothing is blocked automatically.

([#47](https://github.com/antonyrag/ragleap-core/pull/47))

## [0.4.5]

### Added
- Metadata filtering — `metadata_filter=` on `ask()`, JSONB containment query, GIN-indexed. The multi-tenant isolation mechanism.

### Known limitation (still open)
- `update_document()` does not forward the original document's metadata to the re-ingested version — silently resets to `{}` unless the caller passes `metadata=` again. Documented, not yet fixed.

([#46](https://github.com/antonyrag/ragleap-core/pull/46))

## [0.4.4]

### Added
- Document lifecycle: `list_documents()`, `delete_document()`, `update_document()`. `update_document()` is delete + re-ingest under the hood — chunk boundaries and embeddings are not preserved, and the document gets a new `document_id`.

([#45](https://github.com/antonyrag/ragleap-core/pull/45))

## [0.4.3]

### Added
- Async support: `aingest`, `aingest_text`, `aask`, `aask_stream` — wrap the sync implementation in `asyncio.to_thread()` so a blocking DB/embedding call doesn't stall an async web server's event loop.

([#44](https://github.com/antonyrag/ragleap-core/pull/44))

## [0.4.2]

### Added
- Query embedding caching — in-memory LRU (1000 entries by default), opt-out via `cache_enabled=False`. Caches embeddings only, never full answers (conversation memory means the same question can legitimately produce different answers depending on session history).

([#43](https://github.com/antonyrag/ragleap-core/pull/43))

## [0.4.1]

### Changed
- Connection pooling for all database access — previously every `ingest`/`ask`/memory operation opened a fresh Postgres connection. Automatic, no configuration needed.

([#41](https://github.com/antonyrag/ragleap-core/pull/41))

## [0.4.0]

### Added
- Cross-encoder reranking (`rerank=True` on `ask()`) — opt-in, requires the `[rerank]` extra (pulls in `sentence-transformers` + `torch`).

([#39](https://github.com/antonyrag/ragleap-core/pull/39), docs: [#40](https://github.com/antonyrag/ragleap-core/pull/40))

## [0.3.0]

### Added
- Persistent conversation memory — session-scoped, Postgres-backed. Pass `session_id=` to `ask()`/`ask_stream()` for multi-turn context; omit it for a stateless call (identical to prior behavior).

([#37](https://github.com/antonyrag/ragleap-core/pull/37), docs fix: [#38](https://github.com/antonyrag/ragleap-core/pull/38))

## [0.2.0]

### Added
- `EmbeddingConfig` — explicit, pluggable embedding provider configuration (Gemini, OpenAI).

### Breaking
- Replaced the old `embedder_api_key` constructor parameter with `EmbeddingConfig`. Anyone upgrading from 0.1.0 needs to change:
```python
  # Before (0.1.0)
  RagLeap(database_url=..., embedder_api_key="...")

  # After (0.2.0+)
  RagLeap(database_url=..., embedder=EmbeddingConfig(provider="gemini", api_key="..."))
```

([#36](https://github.com/antonyrag/ragleap-core/pull/36))

## [0.1.0] - Initial release

### Added
- `ragleap-rag` — standalone, pip-installable RAG package (Phase A of the `ragleap-core` open-source effort). Core loop: `ingest`/`ingest_text` → chunk → embed → store in Postgres+pgvector; `ask()` for grounded question answering. Gemini and Anthropic supported as generation providers, Gemini/OpenAI for embeddings.

([#35](https://github.com/antonyrag/ragleap-core/pull/35))

[Unreleased]: https://github.com/antonyrag/ragleap-core/compare/main...HEAD
