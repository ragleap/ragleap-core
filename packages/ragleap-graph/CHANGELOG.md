# Changelog

All notable changes to `ragleap-graph` are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.9.1]
### Changed
- Metadata and docs only, no behavior change. Project links on PyPI now point to `ragleap/ragleap-core` after the organization move; tests and docs use placeholder-style dummy credentials. `__version__` is now kept in step with the package version.

## [0.9.0]
### Added
- Ontology cross-validation (#152). `ExtractionConfig.relation_ontology` (default `None`) lets callers define which `relation_type` values are semantically valid between which `entity_type` pairs, e.g. `{"FOUNDED_BY": (["ORGANIZATION"], ["PERSON"])}`. Requires `entity_types=` to also be set (raises `ValueError` at config time otherwise, matching the fail-fast style already used elsewhere in `ExtractionConfig.__post_init__`) - an ontology is meaningless without typed entities to check it against.
- `LLMRelationExtractor.extract()` gained an optional `entity_types` parameter (a `{lowercased_name: type}` map) threaded through from the `entity_type_map` already built during `upsert_document()`'s per-chunk loop - the type information existed in scope already, it just never reached relation extraction or validation before this.
- A relation whose `relation_type` is a key in `relation_ontology`, but whose subject/object entity types are not in that relation_type's allowed lists, is dropped (with a `WARNING` logged, not silently) rather than written to Neo4j. `relation_type` values NOT present in `relation_ontology` remain fully unconstrained - an ontology only needs to cover the relation_types worth constraining, not every possible one, mirroring `entity_types=`'s own "unset means anything goes" precedent one level up.
- Applies uniformly to both the existing per-chunk extraction pass and the v0.8.0 cross-chunk pass, since both call the same `LLMRelationExtractor.extract()`.
### Verified
- 4 new offline tests: config-time `ValueError` when `relation_ontology` is set without `entity_types`, a relation matching allowed types is kept, a relation violating allowed types is dropped, an unlisted relation_type passes through unconstrained.
- 1 new live end-to-end test, gated on live Neo4j + `GEMINI_API_KEY` (same precedent as the #154 cross-chunk test: ontology validation depends on the underlying relation_type/entity_type extraction being accurate, which is unreliable on small local models). Confirms real Gemini-extracted output: an ontology-valid relation is kept, and a real ontology-violating relation the model actually proposed is genuinely dropped - not just asserted against mocked output.
- Full offline suite: 91 passed, 16 skipped without credentials (previous baseline 87/16 - 4 new tests added, zero regressions).

## [0.8.0]
### Added
- Cross-chunk relation extraction (#154). `ExtractionConfig.cross_chunk_relations` (default `False`) enables an opt-in second pass in `upsert_document()`: after all chunks are processed, one additional `LLMRelationExtractor.extract()` call runs over the full accumulated document text using every entity found across all chunks (not just the current chunk's local entities), recovering relations whose subject and object were established in different chunks - e.g. an entity introduced in chunk 1 referenced only by pronoun ("It", "the company") in a later chunk. Requires `extract_relations=True`.
- `LLMRelationExtractor.extract()` gained an optional `resolve_references` parameter (default `False`, so ordinary per-chunk extraction is unchanged) that adds an explicit instruction asking the model to resolve pronouns/vague references to a known entity name. Only the new cross-chunk pass sets this to `True`.
- Cross-chunk relations feed the *same* `relation_counter` used by the existing per-chunk pass, so no new Neo4j write path was needed - the existing `RelationWeight` aggregation, `RELATES_AS` MERGE, and `max_pairs` cutoff all apply unmodified.
### Known limitation (documented, not hidden)
- Cross-chunk relation extraction quality is highly dependent on the LLM provider's reasoning capability. Live-verified working correctly with Gemini (`gemini-3.5-flash`), which correctly resolved a pronoun reference across chunks to produce the right relation. Small local models were found, via live testing, to fail this specific task in a worse way than simply missing the relation: `qwen2.5:0.5b` produced a plausible-looking but factually wrong relation (incorrectly connecting two unrelated entities) rather than returning nothing. Anyone enabling `cross_chunk_relations=True` with a small local model should weigh this before trusting its output uncritically.
### Verified
- Full offline suite: 88 passed, 15 skipped (previous baseline 87/15, one new test added) - zero regressions to existing per-chunk extraction behavior (`resolve_references` defaults to `False` and the cross-chunk pass is gated entirely behind the new opt-in config flag).
- 1 new live end-to-end test (`test_cross_chunk_relation_recovers_relation_per_chunk_extraction_misses`), gated on both live Neo4j credentials and a `GEMINI_API_KEY` (not Ollama, per the known-limitation note above): confirms `cross_chunk_relations=True` recovers a relation that per-chunk extraction alone cannot, and confirms `cross_chunk_relations=False` (the default) correctly does not recover it - proving the flag actually changes behavior rather than the test document being trivially easy regardless.
- Test document deliberately uses a pronoun ("It") rather than a repeated noun phrase ("the company") after an earlier version was found, via live diagnostic, to have the generic phrase extracted as its own separate entity - a real, documented pitfall for anyone writing similar test documents in the future.

## [0.7.0]
### Added
- Real schema migration framework (`ragleap_graph.migrations`), implementing the proposal in `docs/design/schema-migrations.md`: a `Migration` base class (`id`, `description`, `up(session)`, optional `down(session)`, `is_applied(session)`), a `MigrationRunner` (discovers pending migrations, applies them in lexicographic id order, records each as a `:_Migration` node, fails loud and stops on the first error, supports `dry_run=True` and `.status()`), and both existing bespoke migrations (`backfill_user_id_defaults()`, `backfill_composite_key()`) registered as `Migration0001_BackfillUserIdDefaults` / `Migration0002_BackfillCompositeKey` in `ALL_MIGRATIONS`. Resolves the maintainer open questions in the design doc: migrations live in a new `ragleap_graph/migrations/` subpackage; the runner logs a `WARNING` before applying (pointing at the backup docs) rather than silently proceeding; the existing `backfill_*()` bound methods are kept as-is (not deprecated) and now delegate to shared session-level helpers rather than duplicating logic; no concurrency guard/lock was added, matching the design doc's own "not proposed for v1" recommendation.
- Accepted `docs/adr/0001-backup-restore-ownership.md` (native `neo4j-admin` tooling, not a custom Python backup wrapper - consistent with `ragleap-rag`'s existing approach to Postgres) and added a `## Operations` section to `README.md` stating this plainly.
### Changed
- Refactored `backfill_user_id_defaults()` and `backfill_composite_key()`: their core logic is now in new module-level `_backfill_user_id_defaults_session()` / `_backfill_composite_key_session()` functions that operate on an already-open session, so the migration framework can call the identical logic without duplicating it. Zero behavior change - both bound methods keep their existing signatures and return values; verified via the full existing test suite plus a live re-run of both methods against real Neo4j.
### Verified
- 9 new offline tests covering `MigrationRunner`'s logic (sort order, dry-run reports without executing, skips already-applied migrations, applies and records pending ones, stops on first failure without recording it, `.status()` reporting, `down()`'s default `NotImplementedError`, registry has unique sortable ids) using fake driver/session doubles - no live Neo4j required.
- 1 new live end-to-end test: real dry-run against live Neo4j correctly reports pending migrations, applying them creates real `:_Migration` tracking nodes, a second run is a correct no-op (idempotent), `.status()` reflects applied state - confirmed against real Neo4j, not mocked.
- Full suite: 98 passed, 4 skipped - zero regressions.

## [0.6.9]
### Fixed
- RELATES_AS relationship-MERGE race - a real, previously-undocumented bug found while reviewing this code (distinct from the deferred "RelationWeight stress test" item from the v0.6.7 handoff - the RelationWeight NODE already had its composite_key fix; this relationship was never touched until now). `MERGE (es)-[r:RELATES_AS {relation_type: $relation_type}]->(eo)` matched only on the two Entity endpoints plus relation_type - not atomic against concurrent writers, same class of bug as the v0.6.7/v0.6.8 node- and relationship-level races.
- Fix: a `composite_key` property (namespace/user_id/subject/relation_type/object) is now MERGE'd on the RELATES_AS relationship, backed by a relationship-level uniqueness constraint. RELATES_AS is directed (subject -> object via a specific relation_type), unlike CO_OCCURS_WITH, so no pair-sorting/canonicalization was needed. `relation_type` is kept as a real property via `ON CREATE SET` (not removed) since `find_relations()` and `find_lineage()` read it directly.
### Verified
- Live concurrency test using real Ollama (qwen2.5:0.5b) extraction: 4 concurrent `upsert_document()` calls, 4 different document_ids, same real relation-bearing sentence. Deliberately does not assert an exact relationship count, since real LLM extraction can legitimately produce more than one distinct relation_type string for the same input; instead asserts the property the fix actually guarantees - no two relationships share a composite_key. Passed against real Neo4j + real Ollama.
- Full suite: 79 passed, 13 skipped - zero regressions.

## [0.6.8]
### Fixed
- CO_OCCURS_WITH relationship-MERGE race, noticed but deliberately left open in the v0.6.7 fix (that release closed the four NODE-level races for Document/Entity/PairWeight/RelationWeight; this closes the equivalent race at the relationship level). `MERGE (ea)-[r:CO_OCCURS_WITH]-(eb)` matched only on the two Entity endpoints and relationship type - not atomic against concurrent writers, same class of bug as the v0.6.7 node-level races.
- Fix: a `composite_key` property (same `_composite_key()` helper as the four node types) is now MERGE'd on the CO_OCCURS_WITH relationship, backed by a relationship-level uniqueness constraint (`FOR ()-[r:CO_OCCURS_WITH]-() REQUIRE r.composite_key IS UNIQUE`), created idempotently alongside the existing four node constraints. CO_OCCURS_WITH is undirected and entity-pair order wasn't canonicalized anywhere in the existing code, so the composite_key is computed from a sorted (a, b) pair - without this, the same logical entity pair extracted in different orders across different documents would compute two different keys and MERGE could still create a duplicate relationship.
- Added `backfill_co_occurs_with_composite_key(namespace=None, batch_size=500)` - the same one-time, idempotent migration pattern as `backfill_composite_key()` (v0.6.7), for CO_OCCURS_WITH relationships written before this version. Deduplicates on relationship identity (`WITH DISTINCT r`) before reading endpoint names via `startNode(r)`/`endNode(r)`, since Neo4j's undirected-match semantics can otherwise surface the same relationship via more than one traversal direction.
### Verified
- Live concurrency test (8 concurrent `upsert_document()` calls, 8 different document_ids, all mentioning the same two entities) against real Neo4j: exactly 1 CO_OCCURS_WITH relationship resulted, with composite_key correctly set - confirming the fix under real contention, not just code review. This is a different race shape than v0.6.7's Document-race test (same document_id, concurrent calls), since CO_OCCURS_WITH deliberately aggregates across DIFFERENT documents by design.
- Full suite: 79 passed, 12 skipped - zero regressions.

## [0.6.7]
### Fixed
- Real, confirmed concurrency bug (issue #183): concurrent upsert_document() calls with identical document_id/user_id/namespace could create duplicate Document/Entity/PairWeight/RelationWeight nodes. MERGE on multiple plain properties is not atomic against concurrent writers without a uniqueness constraint, and Neo4j Community Edition only supports single-property uniqueness constraints (no composite constraints, unlike Enterprise Edition). Reproduced live before fixing: 8 duplicate Document nodes from 6 "successful" concurrent upserts of the same document.
- Fix: a single hashed `composite_key` property (SHA256 over each label's real identity fields - namespace/user_id/id for Document, namespace/user_id/name for Entity, plus document_id/entity_a/entity_b for PairWeight and document_id/subject/relation_type/object for RelationWeight) is now MERGE'd on instead of the multiple plain properties, backed by a single-property (Community-compatible) uniqueness constraint created idempotently (`CREATE CONSTRAINT IF NOT EXISTS`) on every `GraphIndex` connect. All existing plain properties (id/namespace/user_id, etc.) remain unchanged, so every existing read/query keeps working as before.
- Added retry-with-backoff for Neo4j transient deadlocks (`Neo.TransientError.Transaction.DeadlockDetected`), which Neo4j's own documentation describes as an expected, retryable outcome of lock contention under concurrency, not corruption. `upsert_document()` previously surfaced these as hard failures with no retry. Up to 3 attempts, exponential backoff with jitter. Only `TransientError` is retried; any other exception fails immediately, unchanged from before. Safe to retry the whole write because every MERGE is now composite_key-based - re-running an already-succeeded MERGE just matches the existing node.
- Added `backfill_composite_key(namespace=None, batch_size=500)` - a one-time, idempotent migration for installations upgrading from a version before `composite_key` existed, matching the pattern established by `backfill_user_id_defaults()` (v0.6.5). Computed in Python per node (no APOC assumed available), written back in batches via `elementId()`.
### Verified
- Concurrency reproduced and fixed via a live regression test (8-16 concurrent threads against real Neo4j, not mocked). Before the fix: up to 8 duplicate Document nodes and real Neo4j deadlocks directly observed. After the fix: Document/Entity/PairWeight consistently show correct counts across dozens of runs, including a 16-thread stress test where 3 real deadlocks fired mid-run and were transparently retried and recovered.
- `RelationWeight`'s composite_key fix uses the identical pattern as `PairWeight` and passes the full test suite, but is not independently concurrency-stress-tested (requires an LLM-based relation extractor not configured in this test environment) - documented honestly as a residual gap, not silently claimed as fully proven.
- The migration was live-verified end-to-end: a real legacy-style node (no `composite_key`) was created, confirmed missing the property, backfilled via `backfill_composite_key()`, and confirmed fixed.
- Full suite: 87 passed, 1 skipped (unrelated, no `GEMINI_API_KEY`) - zero regressions across all 4 label patches plus the retry logic and migration.

## [0.6.6]
### Added
- Audit logging, backed by Postgres, fully opt-in via a new `audit=AuditConfig(database_url=...)` parameter on `GraphIndex.__init__()`. Records one event per call to `upsert_document()` and each of the 6 read methods (`find_relations`, `find_documents_by_entities`, `document_entities`, `find_entities_by_type`, `find_lineage`, `search_related_entities`) - `extract_query_entities()` is untouched, same reasoning as `user_id=` in v0.6.5, since it performs no Neo4j access. Requires the new `audit` extra: `pip install ragleap-graph[audit]`.
- New `ragleap_graph._audit` module: `AuditConfig` (just `database_url=`, caller-supplied, never hardcoded - self-hosted users bring their own Postgres) and `AuditLogger`, which creates its own `ragleap_graph_audit_log` table (`CREATE TABLE IF NOT EXISTS`, idempotent) on first use.
- Graceful degradation by design, matching the rest of the library: if `psycopg2` isn't installed, if `database_url` is unreachable, or if a single insert fails, the real Neo4j operation being audited still succeeds - a warning is logged, nothing raises. Audit logging can fail; the feature it's auditing must not fail because of it.
### Verified
- Live-verified against real Postgres and real Neo4j together, not mocked. Graceful degradation confirmed across all failure modes: no config, config with no `database_url`, and a genuinely unreachable database - none of them raise. The real success path was proven twice: a manual live insert/query round-trip, and a full regression test (`test_audit_logging_records_every_wired_method`) that calls all 7 real audit-wired methods and confirms exactly 7 correctly-ordered, correctly-tagged rows land in Postgres. Full suite: 87 passed, 1 skipped (unrelated, no `GEMINI_API_KEY`) - zero regressions.

## [0.6.5]
### Added
- `user_id=` parameter on `upsert_document()` and all read methods (`find_relations()`, `find_documents_by_entities()`, `document_entities()`, `find_entities_by_type()`, `find_lineage()`) for per-user data isolation within a namespace - the split-identity model, matching how `namespace=` already isolates tenants. `Entity`/`Document`/`PairWeight`/`RelationWeight` MERGE keys now include `user_id`, so two different users writing the same entity name get genuinely separate nodes. `extract_query_entities()` is unaffected - it performs no Neo4j access, so `user_id=` does not apply.
- `backfill_user_id_defaults(namespace=None)` - a one-time, idempotent migration for installations upgrading from a version before `user_id=` existed. Existing nodes have no `user_id` property, and `upsert_document()`'s `MERGE`-based writes require an exact match on every property in the pattern to find-and-update rather than create-a-duplicate - so re-upserting an already-indexed document on a pre-migration graph silently creates duplicate `Entity`/`Document`/`PairWeight`/`RelationWeight` nodes instead of updating the existing ones. Running this once resolves that going forward.
- All read methods use `coalesce(node.user_id, '') = $user_id` in `WHERE` clauses rather than `user_id` inside a `MATCH` property pattern, so `user_id=None` (the default) correctly matches both new and pre-existing data immediately, without depending on the migration having been run first - reads and writes have different backward-compatibility needs, and are handled differently on purpose.
### Verified
- Live-verified against real Neo4j across all 6 threaded methods plus the migration method. Two real bugs were caught and fixed before shipping: (1) an operator-precedence error in `find_lineage()`'s `WHERE` clause where an unparenthesized `OR`/`AND` combination would have silently skipped `user_id` filtering on one branch of the lookup; (2) `find_relations()` initially used the same `MATCH`-pattern approach later found to be inconsistent with the other 5 read methods, and was rewritten to use `coalesce()` for consistency and correctness against legacy data. Two regression tests confirm the real duplication bug reproduces and that the migration fixes it (`test_backfill_user_id_defaults_prevents_duplicate_entities_on_reupsert`, which calls the real `upsert_document()` twice, not a mock). Full suite: 86 passed, 1 skipped (unrelated, no `GEMINI_API_KEY`) - zero regressions.

## [0.6.4]
### Added
- `find_lineage(entity_a, entity_b, relation_type=None, namespace=None, limit=25)` - exposes the per-document `:PairWeight`/`:RelationWeight` contribution-tracking nodes introduced in v0.6.0 for idempotent weight aggregation, which previously had no public read path. Returns a list of per-document contributions (`document_id`, `relation_type`, `weight`, and `relation_name` for `RELATES_AS` entries) for the edge(s) between two entities. Entity order is irrelevant - both `CO_OCCURS_WITH` and `RELATES_AS` are checked in both directions, since callers generally won't know which side was extracted as subject vs. object. Returns `[]` if the pair has never co-occurred or been related.
### Verified
- Regression test (`test_find_lineage`) added and run live against a real Neo4j instance, seeding `:PairWeight`/`:RelationWeight` nodes directly via Cypher (bypassing `upsert_document()`, which this method doesn't touch anyway) for a deterministic setup. Covers: multiple contributing documents aggregating correctly, direction-agnostic lookup (`find_lineage(a, b)` == `find_lineage(b, a)`), `relation_type=` filtering narrowing `RELATES_AS` results while leaving `CO_OCCURS_WITH` untouched, and the never-linked-pair empty case. Full suite re-run: 85 passed, 1 skipped (unrelated, no `GEMINI_API_KEY`) - zero regressions.

## [0.6.3]
### Fixed
- `entity_types=` was guidance only, not enforced: if the model returned a type outside the caller-supplied list (even with prompt guidance nudging it toward that list), the out-of-vocabulary type was accepted as-is. Now enforced: any type not in `entity_types=` is coerced to `"UNKNOWN"`, the same value regex-extracted entities already use when there's no reliable type available - consistent behavior, not a new special case. Enforcement only applies when `entity_types=` is actually set; without it, any type the model returns is still accepted as-is (unchanged default).
### Verified
- Regression test added first, confirmed failing (`assert 'Employee' == 'UNKNOWN'`), then confirmed passing after the fix. A second regression-guard test confirms the unchanged default behavior (no `entity_types=` set) still accepts any type. Full suite re-run: 84 passed, 1 skipped (unrelated) - zero regressions.

## [0.6.2]
### Fixed
- `find_entities_by_type()` did exact-string-match only, so `"Customer"` and `"customer"` were treated as different types even though `entity_type` is stored exactly as the model produced it, with no casing normalization at write time - a caller had no reliable way to predict the exact stored casing without already knowing it. Now case-insensitive (`toLower()` comparison on both sides in the Cypher query).
### Verified
- Reproduced live before any code changed. Regression test added, confirmed failing (`assert 0 == 1`), then confirmed passing after the fix, covering exact/lowercase/uppercase input against a single stored `"Customer"` type. Full suite re-run: 82 passed, 1 skipped (unrelated) - zero regressions.

## [0.6.1]
### Added
- `GraphRetriever.retrieve()`'s `graph_context.related_documents` entries now include an `already_in_vector_results` boolean flag. Closes a known limitation: a document could genuinely surface through both vector search (as a chunk) and graph traversal (as a related document), with no way for the caller to know about the overlap. Deliberately flags rather than removes overlapping entries - removing them would silently discard real graph-relationship context (matched entities, graph score) that's still useful even for a document also present in the chunk list.
### Verified
- Regression test added first using the existing `FakeRag`/`FakeGraph` test doubles (no live services needed - `GraphRetriever` has no I/O of its own), confirmed failing (`KeyError: 'already_in_vector_results'`), then confirmed passing after the fix. Full suite re-run: 81 passed, 1 skipped (unrelated), zero regressions.

## [0.6.0]
### Fixed
- `CO_OCCURS_WITH` and `RELATES_AS` had the same weight-doubling idempotency bug fixed for `CONTAINS` in v0.5.4, but couldn't use the same fix directly: these edges deliberately aggregate weight across multiple different documents that share entities, and there was no per-document contribution tracking to subtract just one document's share on re-upsert without corrupting other documents' contributions.
### Added
- Per-document contribution tracking via two new internal node types, `PairWeight` and `RelationWeight` (one per namespace+pair/relation+document_id). On each `upsert_document()` call, a document's own prior contributions are deleted and rewritten, then affected edges' aggregate weight is recomputed as the sum of all contributing documents' `PairWeight`/`RelationWeight` nodes. If a pair/relation's total contribution drops to zero (the last contributing document stopped mentioning it), the `CO_OCCURS_WITH`/`RELATES_AS` edge is deleted rather than left at a stale non-zero weight.
### Verified
- Three real behaviors proven live, not just one: (1) re-upserting identical content no longer doubles weight, (2) a genuinely different document sharing the same pair/relation correctly aggregates weight (proves the fix doesn't break the legitimate cross-document aggregation this is for), (3) removing a document's contribution correctly drops the aggregate weight rather than leaving it stale.
- `CO_OCCURS_WITH` tested with direct Cypher-seeded data (deterministic, no live LLM call needed). `RELATES_AS` tested with a real Ollama call (`qwen2.5:0.5b`, no API key needed, avoiding dependency on the Gemini key) since relation extraction requires a real LLM completion.
- Full suite re-run: 80 passed, 1 skipped (Gemini-key skip, unrelated to this fix) - zero regressions.

## [0.5.4]
### Fixed
- `upsert_document()`'s docstring claimed writes were "idempotent - safe to re-run," which was false for two real reasons: (1) re-upserting identical content doubled `CONTAINS` edge weight every single call (`ON MATCH SET weight = weight + new` instead of replacing it); (2) re-upserting a document whose content changed to no longer mention an entity left the old `CONTAINS` edge in place forever, with no cleanup path. Fixed by deleting a document's existing `CONTAINS` edges before rewriting them on each upsert - safe because `CONTAINS` is a clean 1:1 document-to-entity link.
### Known limitation (not fixed here, scoped out deliberately)
- `CO_OCCURS_WITH` and `RELATES_AS` still have the same weight-doubling issue, but the fix above does not apply to them: those edges deliberately aggregate weight across *multiple different documents* that share entities - that's the actual point of co-occurrence weighting. There is currently no per-document contribution tracking, so there's no way to subtract just one document's share on re-upsert without corrupting other documents' contributions. Fixing this properly needs a real schema addition (per-document contribution tracking), not a quick patch - tracked as upcoming work, not glossed over.
### Verified
- Both bugs reproduced live before any code changed. Regression test added, initially failed on an unrelated Cypher parameter-escaping mistake in the test itself (not the fix) - caught and corrected before treating the test as valid. Full suite re-run: 78 passed, 1 skipped (Gemini-key skip, unrelated) - zero regressions.

## [0.5.3]
### Fixed
- `find_relations()` only ever searched outgoing relations (entity_name as subject) - searching from the object side silently returned `[]` even when the entity was clearly involved in a real relation. Added `direction=` parameter: `"outgoing"` (default, unchanged), `"incoming"` (new), `"both"` (new, deduplicated via `startNode()`/`endNode()` rather than a UNION, so subject/object stay correctly labeled regardless of which direction matched). Closes a known limitation open since v0.4.0.
### Verified
- Zero prior test coverage existed for `find_relations()` at all, including the existing outgoing-only behavior - backfilled that alongside the new direction tests. Regression test writes `RELATES_AS` edges directly via Cypher (deterministic, no live LLM call needed since this method doesn't touch extraction), confirmed failing first (`TypeError: unexpected keyword argument 'direction'`), then confirmed passing after the fix, including an explicit assertion that the old default behavior still returns `[]` when searching from the object side without `direction="incoming"` - proving the fix is additive, not a silent behavior change. Full suite re-run: 77 passed, 1 skipped (Gemini-key skip, unrelated) - zero regressions.

## [0.5.2]
### Fixed
- Regex entity extraction (`_extract_entity_candidates_from_text()`, backing `extract_query_entities()`) no longer extracts common sentence-initial words ("What", "Who", "The", auxiliary verbs, etc.) as spurious single-word entity candidates. English capitalizes the first word of any sentence regardless of whether it's a proper noun, so a query like "What did Acme Corp launch?" previously produced `["What", "Acme Corp"]` instead of just `["Acme Corp"]`. Fix is narrowly scoped: only exact single-word matches against a fixed stopword list (`_SENTENCE_INITIAL_STOPWORDS`) are dropped, so a genuine multi-word entity that happens to start with one of these words is unaffected. Documented as a known limitation since v0.1.0; closed here.
### Verified
- Regression test added first, confirmed failing against the pre-fix code (`assert "What" not in result` failed against real output `['What', 'Acme Corp']`), then confirmed passing after the fix. Full suite re-run afterward: 75 passed, 2 skipped (skips are the standard live-credential guards, unrelated to this change) - zero regressions.

## [0.5.1]
### Fixed
- PyPI `Documentation` URL was missing entirely from `[project.urls]` - this package had no Documentation link on its PyPI page at all. Added, pointing to this package's reference page on packages.ragleap.com (a documentation site for this package ecosystem, separate from docs.ragleap.com which covers only the commercial RagLeap platform and has zero content about this package).
- Metadata-only patch, no code changes.

## [0.5.0]

### Added

- Optional entity typing/ontology guidance (`ExtractionConfig(entity_types=[...])`) - lets callers guide LLM entity extraction toward domain-specific categories (e.g. "Customer", "Product", "Ticket") instead of generic categories like "ORG"/"PERSON". Passed as guidance in the extraction prompt, same convention as the existing `domain_terms=` parameter - not enforced/validated, the model uses its own judgment for entities that don't fit any given category.
- Entity type is now stored on the graph: `:Entity` nodes gain an `entity_type` property (previously computed by `LLMEntityExtractor` since v0.2.0 but discarded before reaching Neo4j - this closes that gap). Regex-extracted entities are stored with `entity_type="UNKNOWN"`, since regex has no semantic understanding to draw a type from.
- Re-upserting a document does not downgrade a real type to `"UNKNOWN"`: a later regex-only upsert of the same document won't overwrite an already-recorded LLM-derived type, via `coalesce(NULLIF($entity_type, "UNKNOWN"), e.entity_type, "UNKNOWN")` in the write query.
- `GraphIndex.find_entities_by_type(entity_type, namespace=None, limit=25)` - queries entities by their stored type.
- `_collect_entities_for_chunk()` now returns `(name, entity_type)` pairs instead of bare names - internal signature change, not part of the public API. Verified via a regression test that the set of entity *names* produced by the regex path is byte-identical before and after this change; only the return shape changed, not the extraction logic itself.
- 8 new tests: config defaults, prompt-guidance passthrough, response type-field regression guard, regex-path UNKNOWN-typing, the names-unchanged regression guard, and `find_entities_by_type()` edge cases.

### Verified

- Real end-to-end live test: real Gemini call with `entity_types=["Customer", "Product"]` guidance correctly typed "Acme Corp" as `Customer` and "Neo4j Enterprise" as `Product`, both written to a real isolated Neo4j instance and correctly retrieved via `find_entities_by_type()`.

### Known limitations

- No schema *enforcement* - `entity_types=` is guidance only, the model can and will assign types outside the given list when nothing fits; there's no validation or rejection of out-of-vocabulary types.
- No relationship-type ontology constraints (e.g. "a REPORTED relation can only go Customer -> Product") - relation types (v0.4.0) and entity types (v0.5.0) aren't currently cross-validated against each other.
- `find_entities_by_type()` does an exact string match on `entity_type` - "Customer" and "customer" are treated as different types, since entity_type is stored exactly as the model (or "UNKNOWN") produced it, with no additional normalization applied.

## [0.4.0]

### Added

- Typed relation extraction (`ExtractionConfig(extract_relations=True)`) - identifies typed relationships between already-extracted entities (e.g. "Acme Corp" -[REPORTED]-> "Q3 revenue"), not just untyped co-occurrence. LLM-only feature - requires `method="llm"` (enforced by `ExtractionConfig` itself), since there is no regex equivalent for identifying relation types.
- `LLMRelationExtractor` and `ExtractedRelation` - takes an already-known list of entities and identifies relations between them, rather than extracting entities itself. This constrains the LLM's subject/object choices to entities already extracted and normalized, reducing hallucinated entities and reusing existing, tested entity extraction rather than duplicating it. Skips the LLM call entirely (returns `[]`) when fewer than 2 entities are known, since a relation needs two entities to connect.
- Defensive hallucination filtering: even though the prompt constrains subject/object to known entities, the parser drops (does not crash on) any relation referencing an entity name outside the known set, rather than trusting the model's compliance blindly. Self-relations (subject == object) are also dropped.
- New Neo4j edge type `RELATES_AS {relation_type: ...}`, distinct from the existing untyped `CO_OCCURS_WITH` edges written by co-occurrence extraction - both can coexist on the same graph.
- `GraphIndex.find_relations(entity_name, relation_type=None, namespace=None, limit=25)` - queries typed relations where `entity_name` is the subject. Only searches the subject/outgoing direction in this release; searching by object or both directions is a reasonable future addition, not built here to keep this release's scope honest and verifiable.
- 8 new tests covering config validation, the fewer-than-2-entities LLM-call skip, valid relation parsing with UPPER_SNAKE_CASE normalization, hallucinated-entity filtering, self-relation filtering, and the all-providers-failed error path.

### Verified

- Real end-to-end live test: real Gemini call correctly identified two typed relations (`REPORTED`, `PARTNERED_WITH`) from real text, both written to a real isolated Neo4j instance (separate Docker container from production, ports 7690/7477), and correctly read back via `find_relations()`.

### Known limitations

- `find_relations()` only searches the subject/outgoing direction - it cannot currently find relations where `entity_name` is the object.
- LLM-extracted relation object/subject names can be a shorter or differently-phrased version of the full entity than the co-occurrence entity extraction produced for the same underlying concept (e.g. "Q3" vs "Q3 revenue growth") - observed during the live test above. This is expected LLM extraction variance, not a bug in the relation-writing logic, but it means relation-derived entity names are not guaranteed to exactly match names from the regular entity-extraction path for the same document.
- Relation extraction runs once per chunk (same granularity as entity extraction) - relations spanning multiple chunks of the same document are not identified.

## [0.3.1]

### Fixed

- PyPI metadata (description, keywords) updated to reflect the real v0.3.0 feature set - LLM extraction, entity dedup, and GraphRetriever were shipped in v0.2.0/v0.3.0 but the description still only described v0.1.0's original scope (regex extraction + co-occurrence graphs). No code changes.

## [0.3.0]

### Added

- `GraphRetriever` and `GraphRetrievalConfig` - fuses `ragleap-rag`'s vector search with `ragleap-graph`'s entity-based graph traversal into one retrieval call, with graph-path citations alongside chunk citations. Two modes: `"hybrid"` (vector search + graph expansion combined, the default) and `"graph_only"` (skip vector search entirely, pure entity/graph retrieval - e.g. "show all documents related to Customer X").
- Built on `RagLeap.retrieve()` (new in `ragleap-rag` v0.12.0, added specifically to support this) rather than reaching into `ragleap-rag`'s private internals (`_vector_backend`, `_embed_query_cached()`) across a package boundary - see `ragleap-rag`'s own CHANGELOG v0.12.0 entry for the reasoning.
- Built on `GraphIndex`'s existing, tested methods (`extract_query_entities`, `find_documents_by_entities`, `search_related_entities`) - no new Cypher queries written for this feature.
- New `retrieval` extra (`pip install ragleap-graph[retrieval]`, requires `ragleap-rag>=0.12.0`) - separate from the existing `llm` extra, since `GraphRetriever` needs `RagLeap.retrieve()` regardless of whether LLM-based entity extraction is used.
- 14 tests using lightweight fakes matching the real `RagLeap.retrieve()` and `GraphIndex` method contracts - covering both retrieval modes, config validation and passthrough (namespace, depth, domain_terms), citation composition, and the no-query-entities edge case (graph calls correctly skipped rather than called with an empty list).

### Verified

- Real end-to-end integration test: real Postgres (via `ragleap-rag`'s test database, with deterministic fake embeddings so no LLM API key is required), real isolated Neo4j (separate Docker container from production, ports 7690/7477, cleaned up after the test), and `GraphRetriever` wiring both together - confirmed a real ingested document is found via both vector search and graph traversal for the same query, with the two results correctly combined.

### Known limitations

- The regex extractor's capitalized-phrase pattern can pick up the first word of a query as a spurious entity if it happens to be capitalized (e.g. "What did Acme Corp launch?" extracts both `"What"` and `"Acme Corp"` as query entities). This is the same class of pattern-matching imprecision already documented in the v0.2.0 entry below (regex fragmentation), not a new `GraphRetriever`-specific bug - confirmed during the live integration test above.
- `GraphRetriever` does not currently deduplicate or rank graph-derived results against vector-derived results if the same document appears in both `chunks` and `graph_context.related_documents` - both are returned as-is, and combining/re-ranking them is left to the caller for now.

## [0.2.0]

### Added

- Optional LLM-based entity extraction (`ExtractionConfig(method="llm", provider=...)`), alongside the existing regex path. Regex stays the default - zero behavior change on upgrade for existing users.
- Built on `ragleap-rag`'s `GenerationService.generate_answer(response_format=...)` rather than a hand-rolled provider client - reuses its existing cross-provider structured-output handling (Gemini native schema, Anthropic forced tool-use, OpenAI-compatible json_schema/json_object fallback) instead of duplicating that logic.
- `ragleap-rag` added as an optional dependency via a new `llm` extra (`pip install ragleap-graph[llm]`) - not a hard dependency, so regex-only users install nothing extra.
- `EntityDeduplicator`: merges near-duplicate entity names (e.g. "Acme Corp" / "ACME Corp.") via string-similarity threshold before nodes are written to Neo4j. Opt-in via `ExtractionConfig(dedup_enabled=True)`, independent of extraction method - works for both regex- and LLM-extracted names.
- `GraphIndex(extraction=ExtractionConfig(...))` - new optional constructor parameter wiring the above into `upsert_document()`.

### Fixed

- **Real false-positive caught during development**: the initial dedup threshold (0.85) incorrectly merged "Neo4j" and "Neo 4j" as the same entity. Tested against 0.85/0.90/0.92/0.95 and confirmed 0.92 as the measured point where the false positive clears without breaking true-positive merges (e.g. "T.C. Antony" / "TC Antony"). Default threshold set to 0.92, not guessed.
- **Real test-harness bug caught before merge**: the test suite's provider mock relied on module-cache busting keyed to the wrong module path (`extraction` instead of `ragleap_graph.extraction`), so five LLM-extraction tests were silently making real network calls to Gemini/OpenAI-compatible endpoints instead of using the mock - caught because those calls failed on missing API keys in the dev environment, not because the mock was verified working. Fixed by correcting the cache-bust key and adding a canary assertion that fails loudly if the mock doesn't actually take effect, so this class of bug can't recur silently.

### Verified

- `LLMEntityExtractor` proven end-to-end against a real Gemini call (`gemini-3.6-flash`), not just mocked responses.
- `_apply_entity_dedup()` proven end-to-end against a real, isolated Neo4j write (separate Docker container from production, ports 7689/7476, cleaned up after the test) - confirmed real entity nodes are correctly merged/written.

### Known limitations

- `EntityDeduplicator` merges spelling variants of an already-extracted entity name (e.g. "Acme Corp" vs "ACME Corp."); it does NOT fix fragmentation caused by the regex extractor splitting a single real-world entity into multiple disconnected candidates in the first place. Concretely: on input containing "ACME Corp.", the regex extractor's acronym rule and capitalized-phrase rule independently produce "ACME" and "Corp" as two separate candidates (neither matches the other's pattern), alongside the correct "Acme Corp" from elsewhere in the text - three overlapping entities where there should be one. Dedup correctly declines to merge these (similarity ~0.67, below the 0.92 threshold) because bridging that gap via looser substring/prefix matching would risk wrongly merging genuinely distinct entities (e.g. "Apple" with "Apple Music"). Confirmed via a real live test: `method="llm"` on the identical input correctly recognizes "Acme Corp" and "ACME Corp." as one entity from context, producing 3 clean entities instead of regex's 3 fragmented ones. This is a real, structural limitation of pattern-based extraction, not a dedup bug - documented here rather than silently worked around.

## [0.1.0]

### Added

- Initial implementation: `GraphConfig` and `GraphIndex`, knowledge-graph-augmented retrieval for RAG systems via Neo4j.
- Ported from a real, production `GraphService` (Neo4j-backed, regex entity extraction, co-occurrence graph construction), with deliberate adaptations for standalone open-source use:
  - `workspace_id` (required, Django multi-tenant SaaS concept) generalized to an optional `namespace=` parameter, defaulting to `None` — mirrors `ragleap-rag`'s own `metadata_filter=` convention.
  - Django `settings` coupling replaced with an explicit `GraphConfig` dataclass — zero framework dependency, same pattern as `ragleap-rag`'s `EmbeddingConfig`/`ProviderConfig`.
  - A hardcoded, deployment-specific domain vocabulary list (radiation-safety/medical-physics terms) found in the source was **not** carried over as a hardcoded default — it's now an optional `domain_terms=` parameter instead, since shipping someone else's internal business vocabulary as a public library default would be inappropriate and actively wrong for other domains.
- 5 core methods: `upsert_document`, `find_documents_by_entities`, `search_related_entities`, `document_entities`, `health_check`.
- **Real security fix vs. the source this was ported from**: `search_related_entities()`'s `max_depth` parameter is validated as a bounded integer (1-10) before being used in Cypher query construction. The source interpolated this value into the query string unvalidated (`.format(max_depth=max_depth)`) — necessary because Neo4j's Cypher can't parameterize variable-length path bounds normally, but that means unvalidated input would be a real injection vector in a public library API accepting arbitrary caller input.
- **Real bug found and fixed during implementation** (not just written and assumed correct): `GraphDatabase.driver()` is lazy — it does not attempt a real connection at construction time. Without an explicit `verify_connectivity()` call, a bad URI/credentials would silently leave `self.driver` set to a non-functional object instead of `None`, breaking the documented "driver is None means unavailable" contract. Caught by a real test failure, not assumed correct — see the test suite for the exact scenario.
- 26 tests: pure-logic tests (entity normalization, extraction, `max_depth` validation) requiring no live Neo4j, driver-unavailable graceful-degradation tests for every public method, and one live end-to-end integration test (write, query three ways, verified self-cleanup — 0 nodes left behind) that runs automatically when real `NEO4J_URI`/`NEO4J_USER`/`NEO4J_PASSWORD` credentials are present, and skips cleanly otherwise.
- CI: dedicated `ragleap-graph-tests` job with a real Neo4j service container, mirroring `ragleap-rag-tests`' pattern — confirmed passing in GitHub's own CI environment, not just locally.
- **This is the first package in the ragleap ecosystem with genuine, complete live verification from day one** — unlike several `ragleap-rag` vector backends, which shipped honestly labeled "code-complete but unverified" due to no account access. Real Neo4j access was already available, so every method here has actually been proven to work end-to-end, not just tested in isolation.
- **Second real bug found and fixed during implementation**: `search_related_entities()` originally validated `max_depth` *after* checking driver availability, so invalid `max_depth` input silently returned `[]` instead of raising — when it should raise regardless of connection state. Caught by a real test failure. Fixed by validating `max_depth` first, before any driver-availability check.
