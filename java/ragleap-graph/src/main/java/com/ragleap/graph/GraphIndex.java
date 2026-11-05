package com.ragleap.graph;

import java.lang.System.Logger.Level;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ThreadLocalRandom;

/**
 * Neo4j-backed knowledge graph index (Java port of ragleap_graph.GraphIndex), over the HTTP Query API.
 * Always safe to construct: if the server cannot be reached, isAvailable() is false and every method returns
 * an empty result (upsertDocument reports an error) instead of throwing.
 */
public final class GraphIndex implements GraphSearch, AutoCloseable {
    /** Hard ceiling on traversal depth for related-entity searches. */
    public static final int MAX_ALLOWED_DEPTH = 10;
    static final String NOT_AVAILABLE = "Neo4j driver not available";
    private static final System.Logger LOG = System.getLogger("com.ragleap.graph");
    private static final int MAX_ATTEMPTS = 3;

    @FunctionalInterface
    interface Sleeper {
        void sleep(double seconds);
    }

    private static final List<String> CONSTRAINTS = List.of(
            "CREATE CONSTRAINT document_composite_key IF NOT EXISTS FOR (d:Document) REQUIRE d.composite_key IS UNIQUE",
            "CREATE CONSTRAINT entity_composite_key IF NOT EXISTS FOR (e:Entity) REQUIRE e.composite_key IS UNIQUE",
            "CREATE CONSTRAINT pairweight_composite_key IF NOT EXISTS FOR (pw:PairWeight) REQUIRE pw.composite_key IS UNIQUE",
            "CREATE CONSTRAINT relationweight_composite_key IF NOT EXISTS FOR (rw:RelationWeight) REQUIRE rw.composite_key IS UNIQUE",
            "CREATE CONSTRAINT co_occurs_with_composite_key IF NOT EXISTS FOR ()-[r:CO_OCCURS_WITH]-() REQUIRE r.composite_key IS UNIQUE",
            "CREATE CONSTRAINT relates_as_composite_key IF NOT EXISTS FOR ()-[r:RELATES_AS]-() REQUIRE r.composite_key IS UNIQUE");

    private static final String MERGE_DOCUMENT = """
            MERGE (d:Document {composite_key: $composite_key})
            ON CREATE SET d.created_at = datetime(), d.id = $document_id,
                d.namespace = $namespace, d.user_id = $user_id
            SET d.title = $title
            """;
    private static final String DELETE_CONTAINS = """
            MATCH (d:Document {id: $document_id, namespace: $namespace, user_id: $user_id})
                  -[r:CONTAINS]->()
            DELETE r
            """;
    private static final String MERGE_ENTITY = """
            MATCH (d:Document {composite_key: $document_composite_key})
            MERGE (e:Entity {composite_key: $entity_composite_key})
            ON CREATE SET e.display_name = $name, e.name = $name_lower,
                e.namespace = $namespace, e.user_id = $user_id
            SET e.entity_type = coalesce(NULLIF($entity_type, "UNKNOWN"), e.entity_type, "UNKNOWN")
            MERGE (d)-[r:CONTAINS]->(e)
            SET r.weight = $weight
            """;
    private static final String OLD_PAIRS = """
            MATCH (pw:PairWeight {namespace: $namespace, document_id: $document_id, user_id: $user_id})
            RETURN pw.entity_a AS a, pw.entity_b AS b
            """;
    private static final String DELETE_PAIRWEIGHTS = """
            MATCH (pw:PairWeight {namespace: $namespace, document_id: $document_id, user_id: $user_id})
            DELETE pw
            """;
    private static final String MERGE_PAIRWEIGHT = """
            MERGE (pw:PairWeight {composite_key: $composite_key})
            ON CREATE SET pw.namespace = $namespace, pw.document_id = $document_id,
                pw.entity_a = $a, pw.entity_b = $b, pw.user_id = $user_id
            SET pw.weight = $weight
            """;
    private static final String RECOMPUTE_CO_OCCURS = """
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
            """;
    private static final String OLD_RELATIONS = """
            MATCH (rw:RelationWeight {namespace: $namespace, document_id: $document_id, user_id: $user_id})
            RETURN rw.subject AS subject, rw.relation_type AS relation_type, rw.object AS object
            """;
    private static final String DELETE_RELATIONWEIGHTS = """
            MATCH (rw:RelationWeight {namespace: $namespace, document_id: $document_id, user_id: $user_id})
            DELETE rw
            """;
    private static final String MERGE_RELATIONWEIGHT = """
            MERGE (rw:RelationWeight {composite_key: $composite_key})
            ON CREATE SET rw.namespace = $namespace, rw.document_id = $document_id,
                rw.subject = $subject, rw.relation_type = $relation_type,
                rw.object = $object, rw.user_id = $user_id
            SET rw.weight = $weight
            """;
    private static final String RECOMPUTE_RELATES_AS = """
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
            """;
    private static final String FIND_DOCUMENTS = """
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
            """;
    private static final String DOCUMENT_ENTITIES = """
            MATCH (d:Document {id: $document_id, namespace: $namespace})
            WHERE coalesce(d.user_id, '') = $user_id
            MATCH (d)-[:CONTAINS]->(e:Entity)
            WHERE coalesce(e.user_id, '') = $user_id
            RETURN e.name AS entity_id,
                   e.display_name AS entity_name
            LIMIT 50
            """;

    private static final String SEARCH_RELATED = """
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
            """;
    private static final String FIND_RELATIONS_OUT = """
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
            """;
    private static final String FIND_RELATIONS_IN = """
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
            """;
    private static final String FIND_RELATIONS_BOTH = """
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
            """;
    private static final String LINEAGE_PAIRS = """
            MATCH (pw:PairWeight {namespace: $namespace})
            WHERE ((pw.entity_a = $a AND pw.entity_b = $b)
                OR (pw.entity_a = $b AND pw.entity_b = $a))
              AND coalesce(pw.user_id, '') = $user_id
            RETURN pw.document_id AS document_id, pw.weight AS weight
            LIMIT $limit
            """;
    private static final String LINEAGE_RELATIONS = """
            MATCH (rw:RelationWeight {namespace: $namespace})
            WHERE ((rw.subject = $a AND rw.object = $b)
                OR (rw.subject = $b AND rw.object = $a))
              AND ($relation_type IS NULL OR rw.relation_type = $relation_type)
              AND coalesce(rw.user_id, '') = $user_id
            RETURN rw.document_id AS document_id,
                   rw.relation_type AS relation_name,
                   rw.weight AS weight
            LIMIT $limit
            """;
    private static final String ENTITIES_BY_TYPE = """
            MATCH (e:Entity {namespace: $namespace})
            WHERE toLower(e.entity_type) = toLower($entity_type)
              AND coalesce(e.user_id, '') = $user_id
            RETURN e.name AS entity_id,
                   e.display_name AS entity_name,
                   e.entity_type AS entity_type
            LIMIT $limit
            """;

    private final CypherRunner runner;
    private final AuditSink audit;
    private final Sleeper sleeper;
    private final ExtractionConfig extractionConfig;
    private final UpsertPlanner.Extraction extraction;

    public GraphIndex(GraphConfig config) {
        this(config, ExtractionConfig.regex(), AuditSink.NOOP);
    }

    public GraphIndex(GraphConfig config, AuditSink audit) {
        this(config, ExtractionConfig.regex(), audit);
    }

    /** An index that extracts entities (and optionally relations) as the given config says; default is regex. */
    public GraphIndex(GraphConfig config, ExtractionConfig extraction) {
        this(config, extraction, AuditSink.NOOP);
    }

    public GraphIndex(GraphConfig config, ExtractionConfig extraction, AuditSink audit) {
        this(open(config), audit, GraphIndex::sleepSeconds, extraction);
    }

    GraphIndex(CypherRunner candidate, AuditSink audit, Sleeper sleeper) {
        this(candidate, audit, sleeper, ExtractionConfig.regex());
    }

    /** For tests and custom runners; a null or unreachable runner makes the index unavailable. */
    GraphIndex(CypherRunner candidate, AuditSink audit, Sleeper sleeper, ExtractionConfig extraction) {
        this.audit = audit == null ? AuditSink.NOOP : audit;
        this.sleeper = sleeper;
        this.extractionConfig = extraction == null ? ExtractionConfig.regex() : extraction;
        // Built before connecting, like Python: a misconfigured LLM setup fails at construction time.
        this.extraction = UpsertPlanner.Extraction.of(this.extractionConfig);
        this.runner = candidate == null ? null : connect(candidate);
    }

    private static CypherRunner open(GraphConfig config) {
        try {
            return new QueryApiRunner(config);
        } catch (RuntimeException e) {
            LOG.log(Level.WARNING, "Failed to initialize Neo4j connection: " + e.getMessage());
            return null;
        }
    }

    private static CypherRunner connect(CypherRunner candidate) {
        try {
            candidate.run("RETURN 1", Map.of());
            LOG.log(Level.INFO, "Neo4j connection initialized successfully");
        } catch (RuntimeException e) {
            LOG.log(Level.WARNING, "Failed to initialize Neo4j connection: " + e.getMessage());
            try {
                candidate.close();
            } catch (RuntimeException ignored) {
                // closing a failed connection is best effort
            }
            return null;
        }
        try {
            for (String constraint : CONSTRAINTS) {
                candidate.run(constraint, Map.of());
            }
        } catch (RuntimeException e) {
            LOG.log(Level.WARNING, "Failed to create composite_key constraints: " + e.getMessage());
        }
        return candidate;
    }

    private static void sleepSeconds(double seconds) {
        try {
            Thread.sleep((long) (seconds * 1000));
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    public boolean isAvailable() {
        return runner != null;
    }

    /** The extraction settings this index was built with. */
    public ExtractionConfig extractionConfig() {
        return extractionConfig;
    }

    public boolean healthCheck() {
        if (runner == null) {
            return false;
        }
        try {
            runner.run("RETURN 1", Map.of());
            return true;
        } catch (RuntimeException e) {
            LOG.log(Level.WARNING, "Graph health check failed: " + e.getMessage());
            return false;
        }
    }

    @Override
    public void close() {
        if (runner != null) {
            runner.close();
        }
        audit.close();
    }

    public List<String> extractQueryEntities(String query, int maxEntities, List<String> domainTerms) {
        return EntityExtraction.extractQueryEntities(query, maxEntities, domainTerms);
    }

    public List<String> extractQueryEntities(String query) {
        return EntityExtraction.extractQueryEntities(query);
    }

    // ------------------------------------------------------------------ upsert

    public UpsertSummary upsertDocument(String documentId, String title, List<Map<String, Object>> chunks) {
        return upsertDocument(documentId, title, chunks, null, null, 80, 150, null);
    }

    public UpsertSummary upsertDocument(String documentId, String title, List<Map<String, Object>> chunks,
                                        String namespace) {
        return upsertDocument(documentId, title, chunks, namespace, null, 80, 150, null);
    }

    /**
     * Indexes one document and its extracted entities. Idempotent (MERGE based). Retries up to three times,
     * with exponential backoff and jitter, on Neo4j transient errors; other errors are not retried.
     */
    public UpsertSummary upsertDocument(String documentId, String title, List<Map<String, Object>> chunks,
                                        String namespace, String userId, int maxEntities, int maxPairs,
                                        List<String> domainTerms) {
        String ns = namespace == null ? "" : namespace;
        String uid = userId == null ? "" : userId;
        UpsertSummary summary = new UpsertSummary(false, documentId, 0, 0, 0, null);
        for (int attempt = 0; attempt < MAX_ATTEMPTS; attempt++) {
            try {
                summary = upsertOnce(documentId, title, chunks, ns, uid, maxEntities, maxPairs, domainTerms);
                break;
            } catch (GraphTransientException e) {
                if (attempt == MAX_ATTEMPTS - 1) {
                    LOG.log(Level.ERROR, "Graph upsert error: transient Neo4j error persisted after "
                            + MAX_ATTEMPTS + " attempts: " + e.getMessage());
                    summary = new UpsertSummary(false, documentId, 0, 0, 0,
                            "Transient Neo4j error persisted after " + MAX_ATTEMPTS + " attempts: " + e.getMessage());
                    break;
                }
                double backoff = 0.1 * Math.pow(2, attempt) + ThreadLocalRandom.current().nextDouble(0, 0.05);
                LOG.log(Level.WARNING, "Transient Neo4j error on attempt " + (attempt + 1) + "/" + MAX_ATTEMPTS
                        + ", retrying in " + String.format("%.2f", backoff) + "s: " + e.getMessage());
                sleeper.sleep(backoff);
            }
        }
        if (summary.success()) {
            Map<String, Object> detail = new LinkedHashMap<>();
            detail.put("relationships_indexed", summary.relationshipsIndexed());
            detail.put("relations_indexed", summary.relationsIndexed());
            audit("upsert_document", uid, ns, documentId, summary.entitiesIndexed(), detail);
        }
        return summary;
    }

    private UpsertSummary upsertOnce(String documentId, String title, List<Map<String, Object>> chunks,
                                     String ns, String uid, int maxEntities, int maxPairs,
                                     List<String> domainTerms) {
        if (runner == null) {
            return new UpsertSummary(false, documentId, 0, 0, 0, NOT_AVAILABLE);
        }
        UpsertPlanner.Plan plan = UpsertPlanner.plan(chunks, maxEntities, maxPairs, domainTerms, extraction);
        int entities = 0;
        int relationships = 0;
        int relations = 0;
        try {
            String docKey = CompositeKey.of(ns, uid, documentId);
            exec(MERGE_DOCUMENT, "composite_key", docKey, "document_id", documentId, "namespace", ns,
                    "user_id", uid, "title", title == null ? "" : title);
            exec(DELETE_CONTAINS, "document_id", documentId, "namespace", ns, "user_id", uid);

            for (Map.Entry<String, Integer> e : plan.topEntities()) {
                String name = e.getKey();
                String lower = PyText.lower(name);
                String type = plan.entityTypes().getOrDefault(lower, "UNKNOWN");
                exec(MERGE_ENTITY, "document_composite_key", docKey,
                        "entity_composite_key", CompositeKey.of(ns, uid, lower),
                        "namespace", ns, "user_id", uid, "name_lower", lower, "name", name,
                        "entity_type", type, "weight", (double) e.getValue());
                entities++;
            }

            Set<UpsertPlanner.PairKey> oldPairs = new LinkedHashSet<>();
            for (Map<String, Object> row : exec(OLD_PAIRS, "namespace", ns, "document_id", documentId,
                    "user_id", uid)) {
                oldPairs.add(new UpsertPlanner.PairKey((String) row.get("a"), (String) row.get("b")));
            }
            exec(DELETE_PAIRWEIGHTS, "namespace", ns, "document_id", documentId, "user_id", uid);
            Set<UpsertPlanner.PairKey> currentPairs = new LinkedHashSet<>();
            for (Map.Entry<UpsertPlanner.PairKey, Integer> e : plan.topPairs()) {
                String a = PyText.lower(e.getKey().a());
                String b = PyText.lower(e.getKey().b());
                currentPairs.add(new UpsertPlanner.PairKey(a, b));
                exec(MERGE_PAIRWEIGHT, "composite_key", CompositeKey.of(ns, uid, documentId, a, b),
                        "namespace", ns, "document_id", documentId, "a", a, "b", b, "user_id", uid,
                        "weight", (double) e.getValue());
            }
            Set<UpsertPlanner.PairKey> allPairs = new LinkedHashSet<>(oldPairs);
            allPairs.addAll(currentPairs);
            for (UpsertPlanner.PairKey p : allPairs) {
                String x = p.a();
                String y = p.b();
                if (PyText.compareCodePoints(x, y) > 0) {
                    x = p.b();
                    y = p.a();
                }
                exec(RECOMPUTE_CO_OCCURS, "namespace", ns, "a", p.a(), "b", p.b(), "user_id", uid,
                        "co_occurs_composite_key", CompositeKey.of(ns, uid, x, y));
                relationships++;
            }

            Set<UpsertPlanner.RelKey> oldRelations = new LinkedHashSet<>();
            for (Map<String, Object> row : exec(OLD_RELATIONS, "namespace", ns, "document_id", documentId,
                    "user_id", uid)) {
                oldRelations.add(new UpsertPlanner.RelKey((String) row.get("subject"),
                        (String) row.get("relation_type"), (String) row.get("object")));
            }
            exec(DELETE_RELATIONWEIGHTS, "namespace", ns, "document_id", documentId, "user_id", uid);
            Set<UpsertPlanner.RelKey> currentRelations = new LinkedHashSet<>();
            for (UpsertPlanner.RelationEntry r : plan.topRelations()) {
                String subject = PyText.lower(r.subject());
                String object = PyText.lower(r.object());
                currentRelations.add(new UpsertPlanner.RelKey(subject, r.relationType(), object));
                exec(MERGE_RELATIONWEIGHT,
                        "composite_key", CompositeKey.of(ns, uid, documentId, subject, r.relationType(), object),
                        "namespace", ns, "document_id", documentId, "subject", subject,
                        "relation_type", r.relationType(), "object", object, "user_id", uid,
                        "weight", (double) r.count());
            }
            Set<UpsertPlanner.RelKey> allRelations = new LinkedHashSet<>(oldRelations);
            allRelations.addAll(currentRelations);
            for (UpsertPlanner.RelKey r : allRelations) {
                exec(RECOMPUTE_RELATES_AS, "namespace", ns, "subject", r.subject(),
                        "relation_type", r.relationType(), "object", r.object(), "user_id", uid,
                        "relates_as_composite_key",
                        CompositeKey.of(ns, uid, r.subject(), r.relationType(), r.object()));
                relations++;
            }
            return new UpsertSummary(true, documentId, entities, relationships, relations, null);
        } catch (GraphTransientException e) {
            throw e;
        } catch (RuntimeException e) {
            LOG.log(Level.ERROR, "Graph upsert error: " + e.getMessage(), e);
            return new UpsertSummary(false, documentId, entities, relationships, relations, e.getMessage());
        }
    }

    // ----------------------------------------------------------------- queries

    public List<DocumentHit> findDocumentsByEntities(List<String> entityNames) {
        return findDocumentsByEntities(entityNames, null, null, 25);
    }

    /** Documents connected to the given entity names, ranked by matched-entity count and summed edge weight. */
    public List<DocumentHit> findDocumentsByEntities(List<String> entityNames, String namespace, String userId,
                                                     int limit) {
        if (runner == null) {
            return List.of();
        }
        List<String> normalized = EntityExtraction.normalizeEntityList(entityNames);
        if (normalized.isEmpty()) {
            return List.of();
        }
        String ns = namespace == null ? "" : namespace;
        String uid = userId == null ? "" : userId;
        try {
            List<DocumentHit> docs = new ArrayList<>();
            for (Map<String, Object> row : exec(FIND_DOCUMENTS, "namespace", ns, "user_id", uid,
                    "entity_names", normalized, "limit", Math.max(1, limit))) {
                Object id = row.get("document_id");
                Object name = row.get("document_name");
                List<String> names = new ArrayList<>();
                if (row.get("matched_entity_names") instanceof List<?> list) {
                    for (Object o : list) {
                        names.add(String.valueOf(o));
                    }
                }
                docs.add(new DocumentHit(id == null ? "" : id.toString(), name == null ? "" : name.toString(),
                        asInt(row.get("matched_entities")), asDouble(row.get("graph_score")), names));
            }
            Map<String, Object> detail = new LinkedHashMap<>();
            detail.put("entity_names", normalized);
            detail.put("result_count", docs.size());
            audit("find_documents_by_entities", uid, ns, null, null, detail);
            return docs;
        } catch (RuntimeException e) {
            LOG.log(Level.ERROR, "Graph document lookup error: " + e.getMessage(), e);
            return List.of();
        }
    }

    public List<RelatedEntity> searchRelatedEntities(List<String> entityNames) {
        return searchRelatedEntities(entityNames, null, null, 2, 10);
    }

    /**
     * Entities related to the given names through graph traversal of up to maxDepth hops (1 to MAX_ALLOWED_DEPTH).
     * The depth is checked first, even when the server is unreachable, because it is formatted into the query text.
     */
    public List<RelatedEntity> searchRelatedEntities(List<String> entityNames, String namespace, String userId,
                                                     int maxDepth, int limit) {
        if (maxDepth < 1 || maxDepth > MAX_ALLOWED_DEPTH) {
            throw new IllegalArgumentException(
                    "max_depth must be between 1 and " + MAX_ALLOWED_DEPTH + ", got " + maxDepth);
        }
        if (runner == null) {
            LOG.log(Level.WARNING, NOT_AVAILABLE);
            return List.of();
        }
        List<String> normalized = EntityExtraction.normalizeEntityList(entityNames);
        if (normalized.isEmpty()) {
            return List.of();
        }
        String ns = namespace == null ? "" : namespace;
        String uid = userId == null ? "" : userId;
        try {
            String query = SEARCH_RELATED.replace("{max_depth}", Integer.toString(maxDepth));
            List<RelatedEntity> related = new ArrayList<>();
            for (Map<String, Object> row : exec(query, "entity_names", normalized, "namespace", ns,
                    "user_id", uid, "limit", Math.max(1, limit))) {
                related.add(new RelatedEntity(str(row.get("entity_id")), str(row.get("entity_name")),
                        str(row.get("relationship")), asInt(row.get("depth"))));
            }
            Map<String, Object> detail = new LinkedHashMap<>();
            detail.put("entity_names", normalized);
            detail.put("max_depth", maxDepth);
            detail.put("result_count", related.size());
            audit("search_related_entities", uid, ns, null, null, detail);
            return related;
        } catch (RuntimeException e) {
            LOG.log(Level.ERROR, "Related-entity search error: " + e.getMessage(), e);
            return List.of();
        }
    }

    public List<RelationHit> findRelations(String entityName) {
        return findRelations(entityName, null, null, null, 25, "outgoing");
    }

    /** Typed relations involving an entity; direction is "outgoing", "incoming" or "both". */
    public List<RelationHit> findRelations(String entityName, String relationType, String namespace, String userId,
                                           int limit, String direction) {
        if (!"outgoing".equals(direction) && !"incoming".equals(direction) && !"both".equals(direction)) {
            throw new IllegalArgumentException("direction must be \"outgoing\", \"incoming\", or \"both\", got "
                    + (direction == null ? "None" : "'" + direction + "'"));
        }
        if (runner == null) {
            LOG.log(Level.WARNING, NOT_AVAILABLE);
            return List.of();
        }
        String normalized = EntityExtraction.normalizeEntityName(entityName);
        if (normalized.isEmpty()) {
            return List.of();
        }
        String ns = namespace == null ? "" : namespace;
        String uid = userId == null ? "" : userId;
        String query = switch (direction) {
            case "outgoing" -> FIND_RELATIONS_OUT;
            case "incoming" -> FIND_RELATIONS_IN;
            default -> FIND_RELATIONS_BOTH;
        };
        try {
            List<RelationHit> relations = new ArrayList<>();
            for (Map<String, Object> row : exec(query, "subject", PyText.lower(normalized), "namespace", ns,
                    "user_id", uid, "relation_type", relationType, "limit", Math.max(1, limit))) {
                relations.add(new RelationHit(str(row.get("subject")), str(row.get("relation_type")),
                        str(row.get("object")), asDouble(row.get("weight"))));
            }
            Map<String, Object> detail = new LinkedHashMap<>();
            detail.put("entity_name", entityName);
            detail.put("direction", direction);
            detail.put("result_count", relations.size());
            audit("find_relations", uid, ns, null, null, detail);
            return relations;
        } catch (RuntimeException e) {
            LOG.log(Level.ERROR, "Relation search error: " + e.getMessage(), e);
            return List.of();
        }
    }

    public List<LineageEntry> findLineage(String entityA, String entityB) {
        return findLineage(entityA, entityB, null, null, null, 25);
    }

    /**
     * Per-document contributions to the edges between two entities (order does not matter). The names are only
     * lower-cased, not normalized, as in the Python package.
     */
    public List<LineageEntry> findLineage(String entityA, String entityB, String relationType, String namespace,
                                          String userId, int limit) {
        if (runner == null) {
            LOG.log(Level.WARNING, NOT_AVAILABLE);
            return List.of();
        }
        if (entityA == null || PyText.strip(entityA).isEmpty() || entityB == null || PyText.strip(entityB).isEmpty()) {
            return List.of();
        }
        String ns = namespace == null ? "" : namespace;
        String uid = userId == null ? "" : userId;
        String a = PyText.lower(entityA);
        String b = PyText.lower(entityB);
        int max = Math.max(1, limit);
        try {
            List<LineageEntry> contributions = new ArrayList<>();
            for (Map<String, Object> row : exec(LINEAGE_PAIRS, "namespace", ns, "user_id", uid, "a", a, "b", b,
                    "limit", max)) {
                contributions.add(new LineageEntry(str(row.get("document_id")), "CO_OCCURS_WITH", null,
                        boxed(row.get("weight"))));
            }
            for (Map<String, Object> row : exec(LINEAGE_RELATIONS, "namespace", ns, "user_id", uid, "a", a,
                    "b", b, "relation_type", relationType, "limit", max)) {
                contributions.add(new LineageEntry(str(row.get("document_id")), "RELATES_AS",
                        str(row.get("relation_name")), boxed(row.get("weight"))));
            }
            Map<String, Object> detail = new LinkedHashMap<>();
            detail.put("entity_a", entityA);
            detail.put("entity_b", entityB);
            detail.put("result_count", contributions.size());
            audit("find_lineage", uid, ns, null, null, detail);
            return new ArrayList<>(contributions.subList(0, Math.min(max, contributions.size())));
        } catch (RuntimeException e) {
            LOG.log(Level.ERROR, "Lineage lookup error: " + e.getMessage(), e);
            return List.of();
        }
    }

    public List<TypedEntity> findEntitiesByType(String entityType) {
        return findEntitiesByType(entityType, null, null, 25);
    }

    /** Entities of a given type, matched case-insensitively. */
    public List<TypedEntity> findEntitiesByType(String entityType, String namespace, String userId, int limit) {
        if (runner == null) {
            LOG.log(Level.WARNING, NOT_AVAILABLE);
            return List.of();
        }
        if (entityType == null || PyText.strip(entityType).isEmpty()) {
            return List.of();
        }
        String ns = namespace == null ? "" : namespace;
        String uid = userId == null ? "" : userId;
        try {
            List<TypedEntity> entities = new ArrayList<>();
            for (Map<String, Object> row : exec(ENTITIES_BY_TYPE, "namespace", ns, "user_id", uid,
                    "entity_type", entityType, "limit", Math.max(1, limit))) {
                entities.add(new TypedEntity(str(row.get("entity_id")), str(row.get("entity_name")),
                        str(row.get("entity_type"))));
            }
            Map<String, Object> detail = new LinkedHashMap<>();
            detail.put("entity_type", entityType);
            detail.put("result_count", entities.size());
            audit("find_entities_by_type", uid, ns, null, null, detail);
            return entities;
        } catch (RuntimeException e) {
            LOG.log(Level.ERROR, "Entity-by-type search error: " + e.getMessage(), e);
            return List.of();
        }
    }

    private static String str(Object o) {
        return o == null ? null : o.toString();
    }

    private static Double boxed(Object o) {
        return o instanceof Number n ? n.doubleValue() : null;
    }

    /** All entities linked to a specific document (at most 50). */
    public List<EntityRef> documentEntities(String documentId, String namespace, String userId) {
        if (runner == null) {
            return List.of();
        }
        String ns = namespace == null ? "" : namespace;
        String uid = userId == null ? "" : userId;
        try {
            List<EntityRef> entities = new ArrayList<>();
            for (Map<String, Object> row : exec(DOCUMENT_ENTITIES, "document_id", documentId, "namespace", ns,
                    "user_id", uid)) {
                Object id = row.get("entity_id");
                Object name = row.get("entity_name");
                entities.add(new EntityRef(id == null ? null : id.toString(), name == null ? null : name.toString()));
            }
            Map<String, Object> detail = new LinkedHashMap<>();
            detail.put("result_count", entities.size());
            audit("document_entities", uid, ns, documentId, null, detail);
            return entities;
        } catch (RuntimeException e) {
            LOG.log(Level.ERROR, "Document entity lookup error: " + e.getMessage(), e);
            return List.of();
        }
    }

    // ----------------------------------------------------------------- helpers

    private List<Map<String, Object>> exec(String cypher, Object... keysAndValues) {
        Map<String, Object> params = new LinkedHashMap<>();
        for (int i = 0; i < keysAndValues.length; i += 2) {
            params.put((String) keysAndValues[i], keysAndValues[i + 1]);
        }
        return runner.run(cypher, params);
    }

    private void audit(String action, String userId, String namespace, String documentId, Integer entityCount,
                       Map<String, Object> detail) {
        try {
            audit.log(userId, namespace, action, documentId, entityCount, detail);
        } catch (RuntimeException e) {
            LOG.log(Level.WARNING, "Audit log write failed, continuing without it: " + e.getMessage());
        }
    }

    private static int asInt(Object o) {
        return o instanceof Number n ? n.intValue() : 0;
    }

    private static double asDouble(Object o) {
        return o instanceof Number n ? n.doubleValue() : 0.0;
    }

    // ---- one-time backfills (the registered migration steps live in Migrations) ----

    /** Backfills user_id = "" onto Entity/Document/PairWeight/RelationWeight nodes missing it. Idempotent. */
    public Map<String, Long> backfillUserIdDefaults() {
        return backfillUserIdDefaults(null);
    }

    /** Same, scoped to one namespace (null = whole graph). Returns the number of nodes updated per label. */
    public Map<String, Long> backfillUserIdDefaults(String namespace) {
        return GraphBackfills.userIdDefaults(runnerOrNull(), namespace);
    }

    /** Backfills composite_key onto Document/Entity/PairWeight/RelationWeight nodes missing it. Idempotent. */
    public Map<String, Long> backfillCompositeKey() {
        return backfillCompositeKey(null, GraphBackfills.DEFAULT_BATCH_SIZE);
    }

    public Map<String, Long> backfillCompositeKey(String namespace) {
        return backfillCompositeKey(namespace, GraphBackfills.DEFAULT_BATCH_SIZE);
    }

    /** batchSize = nodes per UNWIND write. As in Python, 0 is rejected and a negative value writes nothing. */
    public Map<String, Long> backfillCompositeKey(String namespace, int batchSize) {
        return GraphBackfills.compositeKey(runnerOrNull(), namespace, batchSize);
    }

    /** Backfills composite_key onto CO_OCCURS_WITH relationships missing it. Returns the number updated. */
    public long backfillCoOccursWithCompositeKey() {
        return backfillCoOccursWithCompositeKey(null, GraphBackfills.DEFAULT_BATCH_SIZE);
    }

    public long backfillCoOccursWithCompositeKey(String namespace) {
        return backfillCoOccursWithCompositeKey(namespace, GraphBackfills.DEFAULT_BATCH_SIZE);
    }

    public long backfillCoOccursWithCompositeKey(String namespace, int batchSize) {
        return GraphBackfills.coOccursCompositeKey(runnerOrNull(), namespace, batchSize);
    }

    // ---- communities (port of the Python community methods; the logic lives in CommunityStore) ----

    /** Communities of the default namespace without writing anything (resolution 1.0, min size 2, at most 200000 edges). */
    public List<Communities.Community> detectCommunities() {
        return detectCommunities(null, null, 1.0, 2, false, 200_000);
    }

    /** Groups the entities of a namespace into communities without writing; IllegalArgumentException for bad arguments. */
    public List<Communities.Community> detectCommunities(String namespace, String userId, double resolution, int minSize,
                                                         boolean includeRelations, int maxEdges) {
        return new CommunityStore(runner, audit).detect(namespace, userId, resolution, minSize, includeRelations, maxEdges);
    }

    public CommunityStore.Refresh refreshCommunities() {
        return refreshCommunities(null, null, 1.0, 2, false, 200_000, null, false, 20);
    }

    /** Detects the communities and stores them as :Community nodes (see CommunityStore.refresh). */
    public CommunityStore.Refresh refreshCommunities(String namespace, String userId, double resolution, int minSize,
                                                     boolean includeRelations, int maxEdges,
                                                     java.util.function.Function<Map<String, Object>, String> summarizer,
                                                     boolean force, int maxSummaryItems) {
        return new CommunityStore(runner, audit).refresh(namespace, userId, resolution, minSize, includeRelations,
                maxEdges, summarizer, force, maxSummaryItems);
    }

    public List<CommunityStore.Hit> findCommunities(List<String> entityNames) {
        return findCommunities(entityNames, null, null, 25, false);
    }

    /** Stored communities, optionally only those containing the given entities. */
    public List<CommunityStore.Hit> findCommunities(List<String> entityNames, String namespace, String userId, int limit,
                                                    boolean includeMembers) {
        return new CommunityStore(runner, audit).find(entityNames, namespace, userId, limit, includeMembers);
    }

    /** The connected runner, or null when the server could not be reached (used by MigrationRunner and tests). */
    CypherRunner runnerOrNull() {
        return isAvailable() ? runner : null;
    }
}
