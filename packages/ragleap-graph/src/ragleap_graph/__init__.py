"""
ragleap-graph: knowledge-graph-augmented retrieval for RAG systems.

Ported from a real, production GraphService (Neo4j-backed, regex entity
extraction, co-occurrence graph construction) with two deliberate
adaptations for standalone open-source use:

1. `workspace_id` (required, Django multi-tenant SaaS concept) becomes
   an optional `namespace=` parameter, defaulting to None — mirrors
   ragleap-rag's own `metadata_filter=` convention.
2. Django `settings` coupling becomes an explicit `GraphConfig`
   dataclass — zero framework dependency, same pattern as ragleap-rag's
   `EmbeddingConfig`/`ProviderConfig`.

A real security fix vs. the source this was ported from: `max_depth` in
`search_related_entities()` is validated as a bounded integer before
being used in Cypher query construction (the underlying Neo4j
variable-length-path syntax can't be parameterized normally, so the
depth value has to be validated before string formatting, not just
trusted).
"""
from __future__ import annotations

import logging
import re
import hashlib
import random
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


def _composite_key(*parts: str) -> str:
    """Deterministic, collision-resistant MERGE key.

    Neo4j Community Edition only supports single-property uniqueness
    constraints, but several node types here (Document, Entity,
    PairWeight, RelationWeight) are logically identified by a
    combination of properties (e.g. id + namespace + user_id). Without
    a real constraint, concurrent MERGE calls on the same logical
    identity can race and create duplicate nodes - confirmed via a real
    concurrency regression test (issue #183): 8 duplicate Document
    nodes from 6 "successful" concurrent upserts of the same document.

    This computes a single hashed key from the real identity fields, so
    a single-property uniqueness constraint (Community-compatible) can
    be placed on it instead of requiring Enterprise Edition's composite
    constraints. Null-byte-joined before hashing so no real value could
    accidentally produce a colliding boundary between fields.
    """
    joined = "\x00".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _backfill_user_id_defaults_session(session, namespace=None):
    """Session-level implementation shared by GraphIndex.backfill_user_id_defaults()
    and the migrations framework (see docs/design/schema-migrations.md,
    Migration0001_BackfillUserIdDefaults). Extracted so both the existing bound
    method and a Migration.up(session) can run the identical logic against an
    already-open session -- behavior is unchanged from before this extraction.
    """
    counts = {
        "Entity": 0,
        "Document": 0,
        "PairWeight": 0,
        "RelationWeight": 0,
    }
    ns_filter = "AND n.namespace = $namespace" if namespace is not None else ""
    for label in list(counts.keys()):
        result = session.run(
            f"""
            MATCH (n:{label})
            WHERE n.user_id IS NULL {ns_filter}
            SET n.user_id = ""
            RETURN count(n) AS updated
            """,
            namespace=namespace or "",
        )
        record = result.single()
        counts[label] = record["updated"] if record else 0
    return counts


def _backfill_composite_key_session(session, namespace=None, batch_size=500):
    """Session-level implementation shared by GraphIndex.backfill_composite_key()
    and the migrations framework (see docs/design/schema-migrations.md,
    Migration0002_BackfillCompositeKey). Extracted so both the existing bound
    method and a Migration.up(session) can run the identical logic against an
    already-open session -- behavior is unchanged from before this extraction.
    """
    counts = {
        "Document": 0,
        "Entity": 0,
        "PairWeight": 0,
        "RelationWeight": 0,
    }
    ns_filter = "AND n.namespace = $namespace" if namespace is not None else ""

    label_specs = {
        "Document": (
            f"MATCH (n:Document) WHERE n.composite_key IS NULL {ns_filter} "
            f"RETURN elementId(n) AS eid, n.namespace AS namespace, "
            f"n.user_id AS user_id, n.id AS id",
            lambda r: _composite_key(
                r["namespace"] or "", r["user_id"] or "", str(r["id"] or "")
            ),
        ),
        "Entity": (
            f"MATCH (n:Entity) WHERE n.composite_key IS NULL {ns_filter} "
            f"RETURN elementId(n) AS eid, n.namespace AS namespace, "
            f"n.user_id AS user_id, n.name AS name",
            lambda r: _composite_key(
                r["namespace"] or "", r["user_id"] or "", str(r["name"] or "")
            ),
        ),
        "PairWeight": (
            f"MATCH (n:PairWeight) WHERE n.composite_key IS NULL {ns_filter} "
            f"RETURN elementId(n) AS eid, n.namespace AS namespace, "
            f"n.user_id AS user_id, n.document_id AS document_id, "
            f"n.entity_a AS entity_a, n.entity_b AS entity_b",
            lambda r: _composite_key(
                r["namespace"] or "", r["user_id"] or "",
                str(r["document_id"] or ""), r["entity_a"] or "", r["entity_b"] or "",
            ),
        ),
        "RelationWeight": (
            f"MATCH (n:RelationWeight) WHERE n.composite_key IS NULL {ns_filter} "
            f"RETURN elementId(n) AS eid, n.namespace AS namespace, "
            f"n.user_id AS user_id, n.document_id AS document_id, "
            f"n.subject AS subject, n.relation_type AS relation_type, "
            f"n.object AS object",
            lambda r: _composite_key(
                r["namespace"] or "", r["user_id"] or "",
                str(r["document_id"] or ""), r["subject"] or "",
                r["relation_type"] or "", r["object"] or "",
            ),
        ),
    }

    for label, (read_query, key_fn) in label_specs.items():
        records = list(session.run(read_query, namespace=namespace or ""))
        updates = [
            {"eid": r["eid"], "composite_key": key_fn(r)} for r in records
        ]
        for i in range(0, len(updates), batch_size):
            batch = updates[i:i + batch_size]
            session.run(
                """
                UNWIND $batch AS row
                MATCH (n) WHERE elementId(n) = row.eid
                SET n.composite_key = row.composite_key
                """,
                batch=batch,
            )
        counts[label] = len(updates)

    return counts

try:
    from neo4j import GraphDatabase
    from neo4j.exceptions import TransientError
except ImportError:
    GraphDatabase = None
    TransientError = None

from ._audit import AuditConfig, AuditLogger

# v0.2.0: optional LLM-based entity extraction and dedup. Importing these
# is always safe with zero new hard dependencies - extraction.py itself
# only requires ragleap-rag if method="llm" is actually selected at
# ExtractionConfig construction time.
from ragleap_graph.extraction import (
    ExtractionConfig,
    ExtractedEntity,
    EntityDeduplicator,
    LLMEntityExtractor,
    ExtractedRelation,
    LLMRelationExtractor,
)
from ragleap_graph.retrieval import GraphRetriever, GraphRetrievalConfig

logger = logging.getLogger(__name__)

__version__ = "0.9.1"

# Hard ceiling on traversal depth — prevents both runaway queries and,
# since max_depth is string-interpolated into Cypher (see note above),
# guards against malformed/malicious input reaching the query text.
MAX_ALLOWED_DEPTH = 10

# v0.5.2: single-word candidates dropped from regex entity extraction.
# English capitalizes the first word of any sentence regardless of
# whether it's a proper noun, so naive capitalized-word matching
# extracts spurious entities like "What" from "What did Acme Corp
# launch?" alongside the genuine "Acme Corp". Narrow by design: only
# filters exact single-word matches equal to one of these, so a real
# multi-word entity that happens to start with one of these words is
# unaffected.
_SENTENCE_INITIAL_STOPWORDS = frozenset({
    "What", "Who", "When", "Where", "Why", "How", "Which",
    "The", "This", "That", "These", "Those",
    "Is", "Are", "Was", "Were", "Do", "Does", "Did",
    "Can", "Could", "Would", "Should", "Will", "May", "Might",
})


@dataclass
class GraphConfig:
    """Connection config for a Neo4j-backed GraphIndex.

    No framework dependency — pass values directly rather than relying
    on ambient settings (Django, env vars, etc.). Callers who want
    env-var-driven config can build one with `GraphConfig(uri=os.environ[...])`
    themselves.
    """
    uri: str = "bolt://localhost:7687"
    user: str = "neo4j"
    password: str = ""


class GraphIndex:
    """
    Neo4j-backed knowledge graph index for entity-augmented retrieval.

    Gracefully degrades if the `neo4j` package isn't installed or the
    driver can't connect — `self.driver` will be None and all query
    methods return empty results rather than raising, matching the
    behavior of the production service this was ported from. Always
    safe to construct; check `health_check()` if you need to confirm
    connectivity before relying on it.
    """

    def __init__(
        self,
        config: Optional[GraphConfig] = None,
        extraction: Optional[ExtractionConfig] = None,
        audit: Optional[AuditConfig] = None,
    ):
        self.config = config or GraphConfig()
        self.driver = None
        self._audit = AuditLogger(audit)

        # v0.2.0: entity-extraction strategy. Defaults to ExtractionConfig()
        # which is method="regex", dedup_enabled=False - identical behavior
        # to v0.1.0 for anyone not explicitly opting in.
        self.extraction = extraction or ExtractionConfig()
        self._llm_extractor = None
        if self.extraction.method == "llm":
            # Constructed eagerly (not lazily on first use) so a
            # misconfigured provider fails at GraphIndex construction
            # time, not silently mid-upsert.
            self._llm_extractor = LLMEntityExtractor(self.extraction)

        # v0.4.0: optional typed relation extraction. Requires
        # extraction.method="llm" (enforced by ExtractionConfig itself) -
        # there is no regex equivalent for identifying relation types.
        self._relation_extractor = None
        if self.extraction.extract_relations:
            self._relation_extractor = LLMRelationExtractor(self.extraction)

        if GraphDatabase is None:
            logger.warning(
                "neo4j package not installed. Install with: pip install ragleap-graph[neo4j] "
                "(or just `pip install neo4j`). Graph queries will be unavailable."
            )
            return

        try:
            self.driver = GraphDatabase.driver(
                self.config.uri,
                auth=(self.config.user, self.config.password),
            )
            # GraphDatabase.driver() is lazy - it does not attempt a real
            # connection until first use. Without an explicit check here,
            # a bad URI/credentials would silently leave self.driver set
            # to a non-functional object instead of None, breaking the
            # "always safe to construct, check driver is None" contract
            # documented on this class.
            self.driver.verify_connectivity()
            logger.info("Neo4j driver initialized successfully")
            # v0.6.7: composite_key uniqueness constraint for Document,
            # closing the concurrency bug confirmed in issue #183.
            # IF NOT EXISTS makes this idempotent - safe to run on every
            # connect, same pattern as the audit logger's lazy table
            # creation.
            try:
                with self.driver.session() as _constraint_session:
                    _constraint_session.run(
                        "CREATE CONSTRAINT document_composite_key IF NOT EXISTS "
                        "FOR (d:Document) REQUIRE d.composite_key IS UNIQUE"
                    )
                    _constraint_session.run(
                        "CREATE CONSTRAINT entity_composite_key IF NOT EXISTS "
                        "FOR (e:Entity) REQUIRE e.composite_key IS UNIQUE"
                    )
                    _constraint_session.run(
                        "CREATE CONSTRAINT pairweight_composite_key IF NOT EXISTS "
                        "FOR (pw:PairWeight) REQUIRE pw.composite_key IS UNIQUE"
                    )
                    _constraint_session.run(
                        "CREATE CONSTRAINT relationweight_composite_key IF NOT EXISTS "
                        "FOR (rw:RelationWeight) REQUIRE rw.composite_key IS UNIQUE"
                    )
                    # Relationship-level composite_key constraint, closing the
                    # same non-atomic-MERGE race for CO_OCCURS_WITH that the
                    # four node-level constraints above closed in v0.6.7 --
                    # noticed but deliberately left open at the time. Canonical
                    # (sorted) entity-pair order is used when computing this
                    # key so the same logical pair always produces the same
                    # key regardless of which order extraction returned the
                    # two entity names in.
                    _constraint_session.run(
                        "CREATE CONSTRAINT co_occurs_with_composite_key IF NOT EXISTS "
                        "FOR ()-[r:CO_OCCURS_WITH]-() REQUIRE r.composite_key IS UNIQUE"
                    )
                    # Relationship-level composite_key constraint for
                    # RELATES_AS -- a real, previously-undocumented race
                    # found while reviewing this code, same class of bug
                    # as CO_OCCURS_WITH's (just directed, no sorting
                    # needed since subject/object order is meaningful).
                    _constraint_session.run(
                        "CREATE CONSTRAINT relates_as_composite_key IF NOT EXISTS "
                        "FOR ()-[r:RELATES_AS]-() REQUIRE r.composite_key IS UNIQUE"
                    )
            except Exception as constraint_exc:
                logger.warning(
                    f"Failed to create Document composite_key constraint: {constraint_exc}"
                )
        except Exception as e:
            logger.warning(f"Failed to initialize Neo4j driver: {e}")
            if self.driver is not None:
                try:
                    self.driver.close()
                except Exception:
                    pass
            self.driver = None

    def close(self) -> None:
        """Close the Neo4j driver connection and the audit log connection, if any."""
        if self.driver:
            self.driver.close()
        self._audit.close()

    def __enter__(self) -> "GraphIndex":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Entity extraction (regex/heuristic-based, not LLM-based — cheap
    # and deterministic, matching the production approach this was
    # ported from)
    # ------------------------------------------------------------------

    def _normalize_entity_name(self, raw_name: str) -> str:
        """Normalize extracted entity text into a stable graph key.

        Ported verbatim from the production implementation this package
        is based on (verified via direct source review).
        """
        if not raw_name:
            return ""
        name = re.sub(r"\s+", " ", str(raw_name)).strip(" \t\r\n.,;:!?()[]{}'\"")
        if len(name) < 3:
            return ""
        # Keep acronyms uppercase (e.g., ALARA, IAEA), title-case normal phrases.
        if name.isupper():
            normalized = name
        else:
            normalized = " ".join(part.capitalize() for part in name.split())
        if len(normalized) > 120:
            normalized = normalized[:120]
        return normalized

    def _extract_entity_candidates_from_text(
        self,
        text: str,
        max_entities: int = 12,
        domain_terms: Optional[List[str]] = None,
    ) -> List[str]:
        """
        Lightweight, deterministic entity extraction from free text.

        Ported from the production implementation this package is based
        on, with one deliberate change: the source had a hardcoded list
        of domain-specific terms (radiation-safety/medical-physics
        vocabulary specific to that deployment's customer). That's not
        appropriate as a hardcoded default in a general-purpose
        open-source package — it would leak deployment-specific business
        context and be irrelevant (or actively wrong) for any other
        domain. Here, `domain_terms=` is an optional parameter instead —
        pass your own domain vocabulary if useful, or omit it entirely
        for fully generic acronym/capitalized-phrase extraction.
        """
        if not text:
            return []

        candidates: List[str] = []
        # 1) Acronyms and all-caps entities.
        candidates.extend(re.findall(r"\b[A-Z]{2,}(?:-[A-Z0-9]+)?\b", text))
        # 2) Capitalized word sequences (person/org/concept-like phrases).
        raw_phrase_candidates = re.findall(
            r"\b(?:[A-Z][a-z]{2,})(?:\s+[A-Z][a-z]{2,}){0,2}\b", text
        )
        # 2b) Drop exact single-word sentence-initial stopwords (see
        # _SENTENCE_INITIAL_STOPWORDS docstring above _extract_entity_candidates_from_text).
        candidates.extend(
            c for c in raw_phrase_candidates if c not in _SENTENCE_INITIAL_STOPWORDS
        )
        # 3) Optional caller-supplied domain terms that may appear lowercase.
        if domain_terms:
            text_lower = text.lower()
            for term in domain_terms:
                if term.lower() in text_lower:
                    candidates.append(term)

        seen = set()
        final: List[str] = []
        for cand in candidates:
            normalized = self._normalize_entity_name(cand)
            if not normalized:
                continue
            key = normalized.lower()
            if key in seen:
                continue
            seen.add(key)
            final.append(normalized)
            if len(final) >= max_entities:
                break
        return final

    def extract_query_entities(
        self, query: str, max_entities: int = 10, domain_terms: Optional[List[str]] = None
    ) -> List[str]:
        """Extract entity candidates from a user query string."""
        return self._extract_entity_candidates_from_text(
            query, max_entities=max_entities, domain_terms=domain_terms
        )

    def _collect_entities_for_chunk(
        self, text: str, max_entities: int, domain_terms: Optional[List[str]]
    ) -> List[Tuple[str, str]]:
        """
        Dispatch entity extraction for one chunk of text, per the
        configured self.extraction.method. Returns (name, entity_type)
        pairs (v0.5.0+) - entity_type is "UNKNOWN" for the regex path
        (no semantic understanding to draw a type from), or whatever
        LLMEntityExtractor determined (e.g. "ORG", "PERSON", or a
        caller-supplied category from ExtractionConfig.entity_types=)
        for the LLM path.

        v0.1.0 regex behavior is otherwise completely unchanged - same
        candidates, just paired with a constant "UNKNOWN" type now
        instead of being returned as bare strings.
        """
        if self.extraction.method == "llm":
            if self._llm_extractor is None:  # pragma: no cover - defensive
                raise RuntimeError(
                    "extraction.method='llm' but no LLMEntityExtractor was "
                    "constructed - this should not happen; please report a bug."
                )
            entities = self._llm_extractor.extract(text, domain_terms=domain_terms)
            return [(e.name, e.type) for e in entities][:max_entities]
        names = self._extract_entity_candidates_from_text(
            text, max_entities=max_entities, domain_terms=domain_terms
        )
        return [(name, "UNKNOWN") for name in names]

    # ------------------------------------------------------------------
    # Core graph operations
    # ------------------------------------------------------------------

    def _upsert_document_once(
        self,
        document_id: str,
        title: str,
        chunks: List[Dict[str, Any]],
        namespace: Optional[str] = None,
        user_id: Optional[str] = None,
        max_entities: int = 80,
        max_pairs: int = 150,
        domain_terms: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Index one document and its extracted entities into Neo4j.

        Builds co-occurrence edges between entities found in the same
        chunk, weighted by how often each entity/pair appears across
        the document. Writes are idempotent (MERGE-based) — safe to
        re-run on the same document.

        `namespace=` is optional; omit it for a single global graph, or
        pass a tenant/project identifier to keep graphs isolated.

        `domain_terms=` is an optional list of lowercase-matchable terms
        specific to your content (e.g. industry jargon) that the default
        acronym/capitalized-phrase heuristic wouldn't catch on its own.

        Returns an indexing summary so callers can log/monitor coverage.
        """
        summary: Dict[str, Any] = {
            "success": False,
            "document_id": str(document_id),
            "entities_indexed": 0,
            "relationships_indexed": 0,
            "relations_indexed": 0,
            "error": None,
        }

        if not self.driver:
            summary["error"] = "Neo4j driver not available"
            return summary

        entity_counter: Counter = Counter()
        pair_counter: Counter = Counter()
        relation_counter: Counter = Counter()
        entity_type_map: Dict[str, str] = {}

        for chunk in chunks or []:
            text = (chunk.get("text") or "").strip()
            if not text:
                continue

            entity_type_pairs = self._collect_entities_for_chunk(
                text, max_entities=12, domain_terms=domain_terms
            )
            if not entity_type_pairs:
                continue

            unique_entities = []
            seen_local = set()
            for ent, ent_type in entity_type_pairs:
                key = ent.lower()
                if key in seen_local:
                    continue
                seen_local.add(key)
                unique_entities.append(ent)
                entity_counter[ent] += 1
                # First type seen for a given entity name wins - later
                # chunks mentioning the same entity don't override an
                # already-recorded type. Simple, deterministic tie-break;
                # entities are normalized/deduped by name already, so a
                # genuinely different type for the "same" name is rare.
                entity_type_map.setdefault(key, ent_type)

            for i in range(len(unique_entities)):
                for j in range(i + 1, len(unique_entities)):
                    a, b = unique_entities[i], unique_entities[j]
                    if a == b:
                        continue
                    pair = tuple(sorted((a, b), key=lambda s: s.lower()))
                    pair_counter[pair] += 1

            if self._relation_extractor is not None:
                relations = self._relation_extractor.extract(
                    text,
                    known_entities=unique_entities,
                    domain_terms=domain_terms,
                    entity_types=entity_type_map,
                )
                for rel in relations:
                    key = (rel.subject, rel.relation_type, rel.object)
                    relation_counter[key] += 1

        if (
            self._relation_extractor is not None
            and getattr(self.extraction, "cross_chunk_relations", False)
        ):
            # Cross-chunk pass (#154): entity_counter already accumulates
            # entity names across every chunk, but each chunk's relation
            # extraction call above only sees that chunk's local entities.
            # One additional call, using the full document text and the
            # full accumulated entity list, recovers relations whose
            # subject/object were established in different chunks. Feeds
            # the same relation_counter so downstream weighting, dedup,
            # and the RelationWeight/RELATES_AS write path need no changes.
            all_entities = list(entity_counter.keys())
            full_text = "\n\n".join(
                (chunk.get("text") or "").strip()
                for chunk in (chunks or [])
                if (chunk.get("text") or "").strip()
            )
            if full_text and len(all_entities) >= 2:
                cross_chunk_relations = self._relation_extractor.extract(
                    full_text,
                    known_entities=all_entities,
                    domain_terms=domain_terms,
                    resolve_references=True,
                    entity_types=entity_type_map,
                )
                for rel in cross_chunk_relations:
                    key = (rel.subject, rel.relation_type, rel.object)
                    relation_counter[key] += 1

        if self.extraction.dedup_enabled and entity_counter:
            entity_counter, pair_counter, relation_counter = self._apply_entity_dedup(
                entity_counter, pair_counter, relation_counter
            )

        top_entities = entity_counter.most_common(max_entities)
        top_pairs = pair_counter.most_common(max_pairs)
        ns = namespace or ""
        uid = user_id or ""

        try:
            with self.driver.session() as session:
                document_composite_key = _composite_key(ns, uid, str(document_id))
                session.run(
                    """
                    MERGE (d:Document {composite_key: $composite_key})
                    ON CREATE SET d.created_at = datetime(), d.id = $document_id,
                        d.namespace = $namespace, d.user_id = $user_id
                    SET d.title = $title
                    """,
                    composite_key=document_composite_key,
                    document_id=str(document_id),
                    namespace=ns,
                    user_id=uid,
                    title=title or "",
                )
                # v0.5.4: delete this document's existing CONTAINS edges
                # before rewriting them. Two real bugs this fixes:
                # (1) re-upserting identical content used to double the
                # weight every time (ON MATCH SET weight = weight + new,
                # contradicting the "idempotent, safe to re-run" claim);
                # (2) re-upserting changed content left stale CONTAINS
                # edges to entities no longer mentioned - they never got
                # cleaned up. Delete-then-rewrite fixes both: CONTAINS is
                # a clean 1:1 document->entity link, so wiping and
                # rebuilding per document is safe.
                #
                # NOT applied to CO_OCCURS_WITH/RELATES_AS - those
                # aggregate weight across MULTIPLE different documents by
                # design (that's the point of co-occurrence weighting),
                # and there is currently no per-document contribution
                # tracking that would let us subtract just this
                # document's share without affecting others. Fixing that
                # properly needs a real schema addition, not a quick
                # patch here - tracked as a known limitation.
                session.run(
                    """
                    MATCH (d:Document {id: $document_id, namespace: $namespace, user_id: $user_id})
                          -[r:CONTAINS]->()
                    DELETE r
                    """,
                    document_id=str(document_id),
                    namespace=ns,
                    user_id=uid,
                )

                for entity_name, weight in top_entities:
                    entity_type = entity_type_map.get(entity_name.lower(), "UNKNOWN")
                    entity_composite_key = _composite_key(ns, uid, entity_name.lower())
                    session.run(
                        """
                        MATCH (d:Document {composite_key: $document_composite_key})
                        MERGE (e:Entity {composite_key: $entity_composite_key})
                        ON CREATE SET e.display_name = $name, e.name = $name_lower,
                            e.namespace = $namespace, e.user_id = $user_id
                        SET e.entity_type = coalesce(NULLIF($entity_type, "UNKNOWN"), e.entity_type, "UNKNOWN")
                        MERGE (d)-[r:CONTAINS]->(e)
                        SET r.weight = $weight
                        """,
                        document_composite_key=document_composite_key,
                        entity_composite_key=entity_composite_key,
                        namespace=ns,
                        user_id=uid,
                        name_lower=entity_name.lower(),
                        name=entity_name,
                        entity_type=entity_type,
                        weight=float(weight),
                    )
                    summary["entities_indexed"] += 1

                # v0.6.0: per-document contribution tracking for
                # CO_OCCURS_WITH, closing the same idempotency gap fixed
                # for CONTAINS in v0.5.4. Neo4j relationship properties
                # can't be nested maps, so per-document contributions are
                # tracked as separate :PairWeight nodes (one per
                # namespace+entity_a+entity_b+document_id), and the
                # shared CO_OCCURS_WITH edge weight is recomputed as the
                # sum of all contributing documents' PairWeight nodes
                # whenever this document's contribution changes. This
                # correctly supports re-upserting the same document
                # (old contribution replaced, not added on top) while
                # still correctly aggregating signal from genuinely
                # different documents that share entities.
                old_pairs_result = session.run(
                    """
                    MATCH (pw:PairWeight {namespace: $namespace, document_id: $document_id, user_id: $user_id})
                    RETURN pw.entity_a AS a, pw.entity_b AS b
                    """,
                    namespace=ns,
                    document_id=str(document_id),
                    user_id=uid,
                )
                old_pairs = {(row["a"], row["b"]) for row in old_pairs_result}

                session.run(
                    """
                    MATCH (pw:PairWeight {namespace: $namespace, document_id: $document_id, user_id: $user_id})
                    DELETE pw
                    """,
                    namespace=ns,
                    document_id=str(document_id),
                    user_id=uid,
                )

                current_pairs = set()
                for (a, b), weight in top_pairs:
                    a_lower, b_lower = a.lower(), b.lower()
                    current_pairs.add((a_lower, b_lower))
                    pairweight_composite_key = _composite_key(
                        ns, uid, str(document_id), a_lower, b_lower
                    )
                    session.run(
                        """
                        MERGE (pw:PairWeight {composite_key: $composite_key})
                        ON CREATE SET pw.namespace = $namespace, pw.document_id = $document_id,
                            pw.entity_a = $a, pw.entity_b = $b, pw.user_id = $user_id
                        SET pw.weight = $weight
                        """,
                        composite_key=pairweight_composite_key,
                        namespace=ns,
                        document_id=str(document_id),
                        a=a_lower,
                        b=b_lower,
                        user_id=uid,
                        weight=float(weight),
                    )

                for (a, b) in old_pairs | current_pairs:
                    a_canon, b_canon = sorted((a, b))
                    co_occurs_composite_key = _composite_key(ns, uid, a_canon, b_canon)
                    session.run(
                        """
                        MATCH (pw:PairWeight {namespace: $namespace, entity_a: $a, entity_b: $b, user_id: $user_id})
                        WITH sum(pw.weight) AS total
                        MATCH (ea:Entity {name: $a, namespace: $namespace, user_id: $user_id})
                        MATCH (eb:Entity {name: $b, namespace: $namespace, user_id: $user_id})
                        FOREACH (_ IN CASE WHEN total > 0 THEN [1] ELSE [] END |
                            MERGE (ea)-[r:CO_OCCURS_WITH {composite_key: $co_occurs_composite_key}]-(eb)
                            ON CREATE SET r.namespace = $namespace, r.user_id = $user_id
                            SET r.weight = total
                        )
                        FOREACH (_ IN CASE WHEN total = 0 THEN [1] ELSE [] END |
                            MERGE (ea)-[r2:CO_OCCURS_WITH {composite_key: $co_occurs_composite_key}]-(eb)
                            DELETE r2
                        )
                        """,
                        namespace=ns,
                        a=a,
                        b=b,
                        user_id=uid,
                        co_occurs_composite_key=co_occurs_composite_key,
                    )
                    summary["relationships_indexed"] += 1

                # Same pattern for RELATES_AS.
                old_relations_result = session.run(
                    """
                    MATCH (rw:RelationWeight {namespace: $namespace, document_id: $document_id, user_id: $user_id})
                    RETURN rw.subject AS subject, rw.relation_type AS relation_type, rw.object AS object
                    """,
                    namespace=ns,
                    document_id=str(document_id),
                    user_id=uid,
                )
                old_relations = {
                    (row["subject"], row["relation_type"], row["object"])
                    for row in old_relations_result
                }

                session.run(
                    """
                    MATCH (rw:RelationWeight {namespace: $namespace, document_id: $document_id, user_id: $user_id})
                    DELETE rw
                    """,
                    namespace=ns,
                    document_id=str(document_id),
                    user_id=uid,
                )

                current_relations = set()
                for (subject, relation_type, obj), weight in relation_counter.most_common(max_pairs):
                    subj_lower, obj_lower = subject.lower(), obj.lower()
                    current_relations.add((subj_lower, relation_type, obj_lower))
                    relationweight_composite_key = _composite_key(
                        ns, uid, str(document_id), subj_lower, relation_type, obj_lower
                    )
                    session.run(
                        """
                        MERGE (rw:RelationWeight {composite_key: $composite_key})
                        ON CREATE SET rw.namespace = $namespace, rw.document_id = $document_id,
                            rw.subject = $subject, rw.relation_type = $relation_type,
                            rw.object = $object, rw.user_id = $user_id
                        SET rw.weight = $weight
                        """,
                        composite_key=relationweight_composite_key,
                        namespace=ns,
                        document_id=str(document_id),
                        subject=subj_lower,
                        relation_type=relation_type,
                        object=obj_lower,
                        user_id=uid,
                        weight=float(weight),
                    )

                for (subject, relation_type, obj) in old_relations | current_relations:
                    relates_as_composite_key = _composite_key(
                        ns, uid, subject, relation_type, obj
                    )
                    session.run(
                        """
                        MATCH (rw:RelationWeight {namespace: $namespace, subject: $subject, relation_type: $relation_type, object: $object, user_id: $user_id})
                        WITH sum(rw.weight) AS total
                        MATCH (es:Entity {name: $subject, namespace: $namespace, user_id: $user_id})
                        MATCH (eo:Entity {name: $object, namespace: $namespace, user_id: $user_id})
                        FOREACH (_ IN CASE WHEN total > 0 THEN [1] ELSE [] END |
                            MERGE (es)-[r:RELATES_AS {composite_key: $relates_as_composite_key}]->(eo)
                            ON CREATE SET r.relation_type = $relation_type, r.namespace = $namespace, r.user_id = $user_id
                            SET r.weight = total
                        )
                        FOREACH (_ IN CASE WHEN total = 0 THEN [1] ELSE [] END |
                            MERGE (es)-[r2:RELATES_AS {composite_key: $relates_as_composite_key}]->(eo)
                            DELETE r2
                        )
                        """,
                        namespace=ns,
                        subject=subject,
                        relation_type=relation_type,
                        object=obj,
                        user_id=uid,
                        relates_as_composite_key=relates_as_composite_key,
                    )
                    summary["relations_indexed"] += 1


            summary["success"] = True
            return summary

        except TransientError:
            # Not caught here - let the public upsert_document()
            # wrapper decide whether to retry. Neo4j documents
            # transient errors (including deadlocks) as expected,
            # retryable outcomes of lock contention, not corruption.
            raise
        except Exception as e:
            logger.error(f"Graph upsert error: {e}", exc_info=True)
            summary["error"] = str(e)
            return summary

    def upsert_document(
        self,
        document_id: str,
        title: str,
        chunks: List[Dict[str, Any]],
        namespace: Optional[str] = None,
        user_id: Optional[str] = None,
        max_entities: int = 80,
        max_pairs: int = 150,
        domain_terms: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Index one document and its extracted entities into Neo4j.
        Public entry point - retries automatically on Neo4j transient
        errors (e.g. deadlocks from concurrent writes to the same
        document/entities). See _upsert_document_once() for the real
        write logic and full docstring.

        v0.6.7: retrying the whole method is safe because every write
        is composite_key/MERGE-based (issue #183 fix) - re-running an
        already-succeeded MERGE just matches the existing node, it
        does not create a duplicate. Up to 3 attempts, exponential
        backoff with jitter. Non-transient errors are NOT retried and
        fail immediately, same as before this version.
        """
        ns = namespace or ""
        uid = user_id or ""
        max_attempts = 3
        summary: Dict[str, Any] = {
            "success": False,
            "document_id": str(document_id),
            "entities_indexed": 0,
            "relationships_indexed": 0,
            "relations_indexed": 0,
            "error": None,
        }

        for attempt in range(max_attempts):
            try:
                summary = self._upsert_document_once(
                    document_id=document_id,
                    title=title,
                    chunks=chunks,
                    namespace=namespace,
                    user_id=user_id,
                    max_entities=max_entities,
                    max_pairs=max_pairs,
                    domain_terms=domain_terms,
                )
                break
            except TransientError as e:
                if attempt == max_attempts - 1:
                    logger.error(
                        f"Graph upsert error: transient Neo4j error "
                        f"persisted after {max_attempts} attempts: {e}",
                        exc_info=True,
                    )
                    summary["error"] = (
                        f"Transient Neo4j error persisted after "
                        f"{max_attempts} attempts: {e}"
                    )
                    break
                backoff = (0.1 * (2 ** attempt)) + random.uniform(0, 0.05)
                logger.warning(
                    f"Transient Neo4j error on attempt {attempt + 1}/"
                    f"{max_attempts}, retrying in {backoff:.2f}s: {e}"
                )
                time.sleep(backoff)

        # Audit fires at most once per logical call, only on success -
        # matching _upsert_document_once()'s original behavior, where
        # the audit call was only ever reached on the success path
        # (never on "no driver" or a hard failure). Retries are
        # transparent to the audit trail: one logical call, at most
        # one audit row, regardless of how many attempts it took.
        if summary.get("success"):
            self._audit.log(
                user_id=uid,
                namespace=ns,
                action="upsert_document",
                document_id=str(document_id),
                entity_count=summary["entities_indexed"],
                detail={
                    "relationships_indexed": summary["relationships_indexed"],
                    "relations_indexed": summary["relations_indexed"],
                },
            )

        return summary

    def find_documents_by_entities(
        self,
        entity_names: List[str],
        namespace: Optional[str] = None,
        user_id: Optional[str] = None,
        limit: int = 25,
    ) -> List[Dict[str, Any]]:
        """Retrieve documents connected to the given entity names, ranked
        by matched-entity count and summed edge weight."""
        if not self.driver:
            return []

        normalized = self._normalize_entity_list(entity_names)
        if not normalized:
            return []

        ns = namespace or ""
        uid = user_id or ""

        try:
            with self.driver.session() as session:
                result = session.run(
                    """
                    MATCH (e:Entity)<-[r:CONTAINS]-(d:Document)
                    WHERE e.namespace = $namespace
                      AND d.namespace = $namespace
                      AND coalesce(e.user_id, '') = $user_id
                      AND coalesce(d.user_id, '') = $user_id
                      AND e.name IN $entity_names
                    RETURN d.id AS document_id,
                           coalesce(d.title, '') AS document_name,
                           count(DISTINCT e) AS matched_entities,
                           sum(coalesce(r.weight, 1.0)) AS graph_score,
                           collect(DISTINCT e.display_name)[0..10] AS matched_entity_names
                    ORDER BY matched_entities DESC, graph_score DESC
                    LIMIT $limit
                    """,
                    namespace=ns,
                    user_id=uid,
                    entity_names=normalized,
                    limit=max(1, int(limit)),
                )

                docs = []
                for row in result:
                    docs.append({
                        "document_id": str(row["document_id"]) if row["document_id"] is not None else "",
                        "document_name": row["document_name"] or "",
                        "matched_entities": int(row["matched_entities"] or 0),
                        "graph_score": float(row["graph_score"] or 0.0),
                        "matched_entity_names": list(row["matched_entity_names"] or []),
                    })
                self._audit.log(
                    user_id=uid,
                    namespace=ns,
                    action="find_documents_by_entities",
                    detail={"entity_names": normalized, "result_count": len(docs)},
                )
                return docs

        except Exception as e:
            logger.error(f"Graph document lookup error: {e}", exc_info=True)
            return []

    def search_related_entities(
        self,
        entity_names: List[str],
        namespace: Optional[str] = None,
        user_id: Optional[str] = None,
        max_depth: int = 2,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        Find entities related to the given entity names via graph
        traversal, up to `max_depth` hops.

        `max_depth` is validated as a bounded integer (1-10) before use
        — Neo4j's Cypher can't parameterize variable-length path bounds
        normally, so this value gets string-formatted into the query.
        Unvalidated input here would be a real injection vector.
        """
        # Validate first, regardless of driver state - invalid input is
        # invalid input whether or not Neo4j happens to be reachable.
        try:
            max_depth = int(max_depth)
        except (TypeError, ValueError):
            raise ValueError(f"max_depth must be an integer, got {max_depth!r}")
        if not (1 <= max_depth <= MAX_ALLOWED_DEPTH):
            raise ValueError(
                f"max_depth must be between 1 and {MAX_ALLOWED_DEPTH}, got {max_depth}"
            )

        if not self.driver:
            logger.warning("Neo4j driver not available")
            return []

        normalized = self._normalize_entity_list(entity_names)
        if not normalized:
            return []

        ns = namespace or ""
        uid = user_id or ""

        try:
            with self.driver.session() as session:
                # max_depth is validated above (bounded int) before
                # being formatted in — Cypher variable-length path
                # bounds can't be passed as query parameters.
                query = """
                    MATCH (start:Entity)
                    WHERE start.name IN $entity_names
                      AND start.namespace = $namespace
                      AND coalesce(start.user_id, '') = $user_id
                    MATCH path = (start)-[*1..{max_depth}]-(related:Entity)
                    WHERE related.namespace = $namespace
                      AND coalesce(related.user_id, '') = $user_id
                    RETURN DISTINCT related.name AS entity_id,
                           related.display_name AS entity_name,
                           type(relationships(path)[0]) AS relationship,
                           length(path) AS depth
                    ORDER BY depth ASC
                    LIMIT $limit
                """.format(max_depth=max_depth)

                result = session.run(
                    query,
                    entity_names=normalized,
                    namespace=ns,
                    user_id=uid,
                    limit=max(1, int(limit)),
                )

                related = []
                for row in result:
                    related.append({
                        "entity_id": row["entity_id"],
                        "entity_name": row["entity_name"],
                        "relationship": row["relationship"],
                        "depth": int(row["depth"]),
                    })
                self._audit.log(
                    user_id=uid,
                    namespace=ns,
                    action="search_related_entities",
                    detail={"entity_names": normalized, "max_depth": max_depth, "result_count": len(related)},
                )
                return related

        except ValueError:
            raise
        except Exception as e:
            logger.error(f"Related-entity search error: {e}", exc_info=True)
            return []

    def find_relations(
        self,
        entity_name: str,
        relation_type: Optional[str] = None,
        namespace: Optional[str] = None,
        user_id: Optional[str] = None,
        limit: int = 25,
        direction: str = "outgoing",
    ) -> List[Dict[str, Any]]:
        """
        Find typed relations involving entity_name (v0.4.0+;
        direction= added v0.5.3, closing a known limitation).
        direction="outgoing" (default, unchanged since v0.4.0):
        entity_name is the subject, e.g.
        (entity_name)-[:PARTNERED_WITH]->(other).
        direction="incoming": entity_name is the object, e.g.
        (other)-[:PARTNERED_WITH]->(entity_name). Previously, searching
        from the object side silently returned [] even when the entity
        was clearly involved in a real relation - this was the actual
        bug, not just a missing feature.
        direction="both": relations in either direction, deduplicated.
        Requires relations to have been written via
        ExtractionConfig(extract_relations=True) during upsert_document() -
        returns [] (not an error) if no typed relations exist, same as
        every other query method here when there is simply nothing to find.
        relation_type= optionally filters to one relation type (e.g.
        "REPORTED"); omit to return all relation types for entity_name.
        """
        if direction not in ("outgoing", "incoming", "both"):
            raise ValueError(
                f'direction must be "outgoing", "incoming", or "both", got {direction!r}'
            )
        if not self.driver:
            logger.warning("Neo4j driver not available")
            return []
        normalized = self._normalize_entity_name(entity_name)
        if not normalized:
            return []
        ns = namespace or ""
        uid = user_id or ""
        try:
            with self.driver.session() as session:
                if direction == "outgoing":
                    query = """
                        MATCH (s:Entity {name: $subject, namespace: $namespace})
                              -[r:RELATES_AS]->(o:Entity {namespace: $namespace})
                        WHERE ($relation_type IS NULL OR r.relation_type = $relation_type)
                          AND coalesce(s.user_id, '') = $user_id
                          AND coalesce(o.user_id, '') = $user_id
                        RETURN s.display_name AS subject,
                               r.relation_type AS relation_type,
                               o.display_name AS object,
                               coalesce(r.weight, 1.0) AS weight
                        ORDER BY weight DESC
                        LIMIT $limit
                    """
                elif direction == "incoming":
                    query = """
                        MATCH (o:Entity {name: $subject, namespace: $namespace})
                              <-[r:RELATES_AS]-(s:Entity {namespace: $namespace})
                        WHERE ($relation_type IS NULL OR r.relation_type = $relation_type)
                          AND coalesce(s.user_id, '') = $user_id
                          AND coalesce(o.user_id, '') = $user_id
                        RETURN s.display_name AS subject,
                               r.relation_type AS relation_type,
                               o.display_name AS object,
                               coalesce(r.weight, 1.0) AS weight
                        ORDER BY weight DESC
                        LIMIT $limit
                    """
                else:
                    query = """
                        MATCH (a:Entity {namespace: $namespace})
                              -[r:RELATES_AS]-(b:Entity {namespace: $namespace})
                        WHERE (a.name = $subject OR b.name = $subject)
                          AND ($relation_type IS NULL OR r.relation_type = $relation_type)
                          AND coalesce(a.user_id, '') = $user_id
                          AND coalesce(b.user_id, '') = $user_id
                        WITH DISTINCT startNode(r) AS s, endNode(r) AS o, r
                        RETURN s.display_name AS subject,
                               r.relation_type AS relation_type,
                               o.display_name AS object,
                               coalesce(r.weight, 1.0) AS weight
                        ORDER BY weight DESC
                        LIMIT $limit
                    """
                result = session.run(
                    query,
                    subject=normalized.lower(),
                    namespace=ns,
                    user_id=uid,
                    relation_type=relation_type,
                    limit=max(1, int(limit)),
                )
                relations = []
                for row in result:
                    relations.append({
                        "subject": row["subject"],
                        "relation_type": row["relation_type"],
                        "object": row["object"],
                        "weight": float(row["weight"]),
                    })
                self._audit.log(
                    user_id=uid,
                    namespace=ns,
                    action="find_relations",
                    detail={"entity_name": entity_name, "direction": direction, "result_count": len(relations)},
                )
                return relations
        except Exception as e:
            logger.error(f"Relation search error: {e}", exc_info=True)
            return []

    def document_entities(
        self,
        document_id: str,
        namespace: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Find all entities linked to a specific document."""
        if not self.driver:
            logger.warning("Neo4j driver not available")
            return []

        ns = namespace or ""
        uid = user_id or ""

        try:
            with self.driver.session() as session:
                result = session.run(
                    """
                    MATCH (d:Document {id: $document_id, namespace: $namespace})
                    WHERE coalesce(d.user_id, '') = $user_id
                    MATCH (d)-[:CONTAINS]->(e:Entity)
                    WHERE coalesce(e.user_id, '') = $user_id
                    RETURN e.name AS entity_id,
                           e.display_name AS entity_name
                    LIMIT 50
                    """,
                    document_id=str(document_id),
                    namespace=ns,
                    user_id=uid,
                )

                entities = []
                for row in result:
                    entities.append({
                        "entity_id": row["entity_id"],
                        "entity_name": row["entity_name"],
                    })
                self._audit.log(
                    user_id=uid,
                    namespace=ns,
                    action="document_entities",
                    document_id=str(document_id),
                    detail={"result_count": len(entities)},
                )
                return entities

        except Exception as e:
            logger.error(f"Document entity lookup error: {e}", exc_info=True)
            return []

    def find_entities_by_type(
        self,
        entity_type: str,
        namespace: Optional[str] = None,
        user_id: Optional[str] = None,
        limit: int = 25,
    ) -> List[Dict[str, Any]]:
        """
        Find entities of a given type (v0.5.0+), e.g. "ORG", "PERSON",
        or a caller-supplied category from ExtractionConfig.entity_types=.

        Only meaningful for entities extracted via method="llm" - regex
        extraction has no semantic understanding to draw a type from, so
        entities from that path are always stored with entity_type
        "UNKNOWN" and will only show up here if entity_type="UNKNOWN" is
        searched for explicitly.

        Matching is case-insensitive (v0.6.2+) - searching "customer"
        finds entities stored with entity_type "Customer" or "CUSTOMER".
        entity_type is stored exactly as the model produced it, with no
        casing normalization at write time, so exact-match-only search
        was previously unreliable for a caller who didn't already know
        the precise stored casing.
        """
        if not self.driver:
            logger.warning("Neo4j driver not available")
            return []

        if not entity_type or not entity_type.strip():
            return []

        ns = namespace or ""
        uid = user_id or ""

        try:
            with self.driver.session() as session:
                result = session.run(
                    """
                    MATCH (e:Entity {namespace: $namespace})
                    WHERE toLower(e.entity_type) = toLower($entity_type)
                      AND coalesce(e.user_id, '') = $user_id
                    RETURN e.name AS entity_id,
                           e.display_name AS entity_name,
                           e.entity_type AS entity_type
                    LIMIT $limit
                    """,
                    namespace=ns,
                    user_id=uid,
                    entity_type=entity_type,
                    limit=max(1, int(limit)),
                )

                entities = []
                for row in result:
                    entities.append({
                        "entity_id": row["entity_id"],
                        "entity_name": row["entity_name"],
                        "entity_type": row["entity_type"],
                    })
                self._audit.log(
                    user_id=uid,
                    namespace=ns,
                    action="find_entities_by_type",
                    detail={"entity_type": entity_type, "result_count": len(entities)},
                )
                return entities

        except Exception as e:
            logger.error(f"Entity-by-type search error: {e}", exc_info=True)
            return []

    def find_lineage(
        self,
        entity_a: str,
        entity_b: str,
        relation_type: Optional[str] = None,
        namespace: Optional[str] = None,
        user_id: Optional[str] = None,
        limit: int = 25,
    ) -> List[Dict[str, Any]]:
        """
        Return per-document contributions to the edge(s) between entity_a
        and entity_b, surfacing the :PairWeight (CO_OCCURS_WITH) and
        :RelationWeight (RELATES_AS) tracking nodes introduced in v0.6.0
        for idempotent weight aggregation. Previously had no public read
        path - this exposes it.

        entity_a/entity_b order does not matter - checked in both
        directions for both relation kinds, since callers generally
        won't know which side was extracted as subject vs. object.

        Returns [] if the pair has never co-occurred or been related.
        """
        if not self.driver:
            logger.warning("Neo4j driver not available")
            return []
        if not entity_a or not entity_a.strip() or not entity_b or not entity_b.strip():
            return []
        ns = namespace or ""
        uid = user_id or ""
        a_lower = entity_a.lower()
        b_lower = entity_b.lower()
        contributions: List[Dict[str, Any]] = []
        try:
            with self.driver.session() as session:
                pair_result = session.run(
                    """
                    MATCH (pw:PairWeight {namespace: $namespace})
                    WHERE ((pw.entity_a = $a AND pw.entity_b = $b)
                        OR (pw.entity_a = $b AND pw.entity_b = $a))
                      AND coalesce(pw.user_id, '') = $user_id
                    RETURN pw.document_id AS document_id, pw.weight AS weight
                    LIMIT $limit
                    """,
                    namespace=ns,
                    user_id=uid,
                    a=a_lower,
                    b=b_lower,
                    limit=max(1, int(limit)),
                )
                for row in pair_result:
                    contributions.append({
                        "document_id": row["document_id"],
                        "relation_type": "CO_OCCURS_WITH",
                        "weight": row["weight"],
                    })

                relation_result = session.run(
                    """
                    MATCH (rw:RelationWeight {namespace: $namespace})
                    WHERE ((rw.subject = $a AND rw.object = $b)
                        OR (rw.subject = $b AND rw.object = $a))
                      AND ($relation_type IS NULL OR rw.relation_type = $relation_type)
                      AND coalesce(rw.user_id, '') = $user_id
                    RETURN rw.document_id AS document_id,
                           rw.relation_type AS relation_name,
                           rw.weight AS weight
                    LIMIT $limit
                    """,
                    namespace=ns,
                    user_id=uid,
                    a=a_lower,
                    b=b_lower,
                    relation_type=relation_type,
                    limit=max(1, int(limit)),
                )
                for row in relation_result:
                    contributions.append({
                        "document_id": row["document_id"],
                        "relation_type": "RELATES_AS",
                        "relation_name": row["relation_name"],
                        "weight": row["weight"],
                    })

            self._audit.log(
                user_id=uid,
                namespace=ns,
                action="find_lineage",
                detail={"entity_a": entity_a, "entity_b": entity_b, "result_count": len(contributions)},
            )
            return contributions[:max(1, int(limit))]
        except Exception as e:
            logger.error(f"Lineage lookup error: {e}", exc_info=True)
            return []

    def backfill_user_id_defaults(self, namespace: Optional[str] = None) -> Dict[str, int]:
        """
        One-time migration (v0.6.5+) for installations upgrading from a
        version before user_id= support existed. Entity/Document/
        PairWeight/RelationWeight nodes written before this version have
        no user_id property at all - Neo4j's exact property-pattern
        matching treats a missing property as never equal to "" (the
        default used when user_id=None), so those legacy nodes would
        otherwise become invisible to every find_* call and would be
        silently duplicated - not updated - the next time
        upsert_document() re-runs on an already-indexed document.
        This backfills user_id="" onto any node still missing the
        property, restoring the documented "user_id=None matches
        everything" backward-compatibility guarantee. Idempotent - safe
        to run multiple times; only touches nodes that still lack the
        property, so re-running after new user_id-aware data has been
        written does not overwrite it.
        Pass namespace= to scope the backfill to one namespace, or omit
        to backfill the whole graph.
        Returns a count of nodes updated per label.
        """
        counts: Dict[str, int] = {
            "Entity": 0,
            "Document": 0,
            "PairWeight": 0,
            "RelationWeight": 0,
        }
        if not self.driver:
            return counts
        with self.driver.session() as session:
            return _backfill_user_id_defaults_session(session, namespace)

    def backfill_composite_key(
        self, namespace: Optional[str] = None, batch_size: int = 500
    ) -> Dict[str, int]:
        """
        One-time migration (v0.6.7+) for installations upgrading from a
        version before composite_key existed. Document/Entity/
        PairWeight/RelationWeight nodes written before this version
        have no composite_key property, so the single-property
        uniqueness constraints added in this version (closing the
        concurrent-duplicate race in issue #183) do not cover them -
        Neo4j uniqueness constraints do not apply to nodes where the
        constrained property is null. This backfills composite_key
        onto any node still missing it, computed identically to how
        every write path computes it, using elementId()-based batched
        writes (no APOC assumed available).

        Idempotent - safe to run multiple times; only touches nodes
        that still lack composite_key, so re-running after new
        composite_key-aware data has been written does not overwrite
        it.

        Pass namespace= to scope the backfill to one namespace, or
        omit to backfill the whole graph. batch_size controls how
        many nodes are updated per UNWIND write - lower it if legacy
        data volume is very large and write transactions are timing
        out.

        Returns a count of nodes updated per label.
        """
        counts: Dict[str, int] = {
            "Document": 0,
            "Entity": 0,
            "PairWeight": 0,
            "RelationWeight": 0,
        }
        if not self.driver:
            return counts
        with self.driver.session() as session:
            return _backfill_composite_key_session(session, namespace, batch_size)
    def backfill_co_occurs_with_composite_key(
        self, namespace: Optional[str] = None, batch_size: int = 500
    ) -> int:
        """
        One-time migration for CO_OCCURS_WITH relationships written
        before composite_key was added to them -- closes the same gap
        backfill_composite_key() closes for the four node labels, just
        at the relationship level (see that method's docstring for the
        general background on why this is needed: uniqueness
        constraints don't apply to null properties, so pre-existing
        data isn't automatically covered by a new constraint).

        Uses the SAME _composite_key() helper and the SAME canonical
        (sorted) entity-pair ordering the live MERGE path uses, so a
        backfilled relationship's key matches exactly what a fresh
        MERGE for that same logical pair would compute.

        CO_OCCURS_WITH is undirected; the read query explicitly
        deduplicates on relationship identity (WITH DISTINCT r) before
        reading endpoint names via startNode(r)/endNode(r), rather
        than relying on assumptions about undirected-match traversal
        semantics -- relationship identity itself is unambiguous
        regardless of how many directions a pattern match considers.

        Idempotent -- only touches relationships still missing
        composite_key, so re-running after new composite_key-aware
        data has been written does not overwrite it.

        Pass namespace= to scope the backfill to one namespace, or
        omit to backfill the whole graph. batch_size controls how
        many relationships are updated per UNWIND write.

        Returns the count of relationships updated.
        """
        if not self.driver:
            return 0

        ns_filter = "AND r.namespace = $namespace" if namespace is not None else ""

        read_query = (
            f"MATCH ()-[r:CO_OCCURS_WITH]-() "
            f"WHERE r.composite_key IS NULL {ns_filter} "
            f"WITH DISTINCT r "
            f"RETURN elementId(r) AS eid, r.namespace AS namespace, "
            f"r.user_id AS user_id, startNode(r).name AS a, endNode(r).name AS b"
        )

        with self.driver.session() as session:
            records = list(session.run(read_query, namespace=namespace or ""))

            updates = []
            for r in records:
                a_canon, b_canon = sorted((r["a"] or "", r["b"] or ""))
                updates.append({
                    "eid": r["eid"],
                    "composite_key": _composite_key(
                        r["namespace"] or "", r["user_id"] or "", a_canon, b_canon
                    ),
                })

            for i in range(0, len(updates), batch_size):
                batch = updates[i:i + batch_size]
                session.run(
                    """
                    UNWIND $batch AS row
                    MATCH ()-[r:CO_OCCURS_WITH]-() WHERE elementId(r) = row.eid
                    SET r.composite_key = row.composite_key
                    """,
                    batch=batch,
                )

        return len(updates)

    def health_check(self) -> bool:
        """Return True if the Neo4j driver is connected and responsive."""
        if not self.driver:
            return False
        try:
            with self.driver.session() as session:
                session.run("RETURN 1")
            return True
        except Exception as e:
            logger.warning(f"Graph health check failed: {e}")
            return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _apply_entity_dedup(
        self, entity_counter: Counter, pair_counter: Counter, relation_counter: Optional[Counter] = None
    ) -> Tuple[Counter, Counter, Counter]:
        """
        Merge near-duplicate entity names (e.g. "Acme Corp" / "ACME Corp.")
        into one canonical node before writing to Neo4j, using
        EntityDeduplicator at the configured self.extraction.dedup_threshold.

        Only called when self.extraction.dedup_enabled is True - this
        is opt-in and independent of extraction method (works for both
        regex- and LLM-extracted entity names).

        relation_counter is optional (v0.4.0+) since not every caller
        has typed relations to remap - defaults to an empty Counter if
        omitted, so callers without relation extraction enabled don't
        need to pass anything new.
        """
        relation_counter = relation_counter if relation_counter is not None else Counter()

        dedup = EntityDeduplicator(threshold=self.extraction.dedup_threshold)
        mapping = dedup.resolve(list(entity_counter.keys()))

        merged_entities: Counter = Counter()
        for name, weight in entity_counter.items():
            canonical = mapping.get(name, name)
            merged_entities[canonical] += weight

        merged_pairs: Counter = Counter()
        for (a, b), weight in pair_counter.items():
            ca = mapping.get(a, a)
            cb = mapping.get(b, b)
            if ca == cb:
                # Both sides of the pair merged into the same canonical
                # entity - no self-loop edge, just drop it.
                continue
            pair = tuple(sorted((ca, cb), key=lambda s: s.lower()))
            merged_pairs[pair] += weight

        merged_relations: Counter = Counter()
        for (subject, relation_type, obj), weight in relation_counter.items():
            c_subject = mapping.get(subject, subject)
            c_object = mapping.get(obj, obj)
            if c_subject == c_object:
                # Both sides merged into the same canonical entity -
                # a self-relation is meaningless, drop it.
                continue
            merged_relations[(c_subject, relation_type, c_object)] += weight

        return merged_entities, merged_pairs, merged_relations

    def _normalize_entity_list(self, entity_names: List[str]) -> List[str]:
        normalized = []
        seen = set()
        for name in entity_names or []:
            norm = self._normalize_entity_name(name)
            if not norm:
                continue
            key = norm.lower()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(key)
        return normalized


__all__ = [
    "GraphConfig",
    "GraphIndex",
    "ExtractionConfig",
    "ExtractedEntity",
    "EntityDeduplicator",
    "LLMEntityExtractor",
    "ExtractedRelation",
    "LLMRelationExtractor",
    "GraphRetriever",
    "GraphRetrievalConfig",
    "__version__",
]
