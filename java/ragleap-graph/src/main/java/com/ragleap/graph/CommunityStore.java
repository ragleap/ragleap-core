package com.ragleap.graph;

import java.lang.System.Logger.Level;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import java.util.function.Function;

/**
 * Community methods of GraphIndex (port of Python's detect_communities, refresh_communities and find_communities):
 * reads the co-occurrence edges, groups them with {@link Communities}, stores the groups as :Community nodes with
 * :IN_COMMUNITY edges, and looks them up again. Use it through GraphIndex. Do not refresh one namespace from two
 * places at once.
 */
public final class CommunityStore {
    static final String CONSTRAINT = "CREATE CONSTRAINT community_composite_key IF NOT EXISTS "
            + "FOR (c:Community) REQUIRE c.composite_key IS UNIQUE";
    static final String CO_OCCURS_EDGES = """
            MATCH (a:Entity {namespace: $namespace})-[r:CO_OCCURS_WITH]-(b:Entity {namespace: $namespace})
            WHERE a.name < b.name
              AND coalesce(a.user_id, '') = $user_id
              AND coalesce(b.user_id, '') = $user_id
            RETURN a.name AS a, b.name AS b, coalesce(r.weight, 1.0) AS weight
            LIMIT $limit
            """;
    static final String RELATES_EDGES = """
            MATCH (a:Entity {namespace: $namespace})-[r:RELATES_AS]-(b:Entity {namespace: $namespace})
            WHERE a.name < b.name
              AND coalesce(a.user_id, '') = $user_id
              AND coalesce(b.user_id, '') = $user_id
            RETURN a.name AS a, b.name AS b, coalesce(r.weight, 1.0) AS weight
            LIMIT $limit
            """;
    static final String EXISTING = """
            MATCH (c:Community {namespace: $namespace})
            WHERE coalesce(c.user_id, '') = $user_id
            RETURN c.composite_key AS key, c.summary IS NOT NULL AS has_summary
            """;
    static final String MERGE = """
            MERGE (c:Community {composite_key: $composite_key})
            ON CREATE SET c.created_at = datetime(), c.namespace = $namespace, c.user_id = $user_id,
                c.members_hash = $members_hash
            SET c.size = $size, c.internal_weight = $internal_weight, c.resolution = $resolution,
                c.updated_at = datetime()
            """;
    static final String MEMBERS = """
            MATCH (c:Community {composite_key: $composite_key})
            UNWIND $members AS member_name
            MATCH (e:Entity {name: member_name, namespace: $namespace})
            WHERE coalesce(e.user_id, '') = $user_id
            MERGE (e)-[:IN_COMMUNITY]->(c)
            """;
    static final String DELETE = """
            MATCH (c:Community {composite_key: $composite_key})
            DETACH DELETE c
            """;
    static final String DISPLAY_NAMES = """
            MATCH (e:Entity {namespace: $namespace})
            WHERE e.name IN $members AND coalesce(e.user_id, '') = $user_id
            RETURN e.name AS name, e.display_name AS display_name
            """;
    static final String RELATIONS = """
            MATCH (s:Entity {namespace: $namespace})-[r:RELATES_AS]->(o:Entity {namespace: $namespace})
            WHERE s.name IN $members AND o.name IN $members
              AND coalesce(s.user_id, '') = $user_id AND coalesce(o.user_id, '') = $user_id
            RETURN s.display_name AS subject, r.relation_type AS relation_type, o.display_name AS object,
                   coalesce(r.weight, 1.0) AS weight
            ORDER BY weight DESC
            LIMIT $limit
            """;
    static final String SET_SUMMARY = """
            MATCH (c:Community {composite_key: $composite_key})
            SET c.summary = $summary, c.summary_updated_at = datetime()
            """;
    static final String LIST = """
            MATCH (c:Community {namespace: $namespace})
            WHERE coalesce(c.user_id, '') = $user_id
            RETURN c.composite_key AS community_id, c.size AS member_count, c.internal_weight AS internal_weight,
                   c.summary AS summary, 0 AS matched_entities
            ORDER BY member_count DESC, community_id ASC
            LIMIT $limit
            """;
    static final String BY_ENTITY = """
            MATCH (e:Entity)-[:IN_COMMUNITY]->(c:Community)
            WHERE e.namespace = $namespace
              AND c.namespace = $namespace
              AND coalesce(e.user_id, '') = $user_id
              AND coalesce(c.user_id, '') = $user_id
              AND e.name IN $entity_names
            RETURN c.composite_key AS community_id, c.size AS member_count, c.internal_weight AS internal_weight,
                   c.summary AS summary, count(DISTINCT e) AS matched_entities
            ORDER BY matched_entities DESC, member_count DESC, community_id ASC
            LIMIT $limit
            """;
    static final String MEMBER_NAMES = """
            MATCH (e:Entity)-[:IN_COMMUNITY]->(c:Community {composite_key: $community_id})
            WHERE coalesce(e.user_id, '') = $user_id
            RETURN e.display_name AS entity_name
            ORDER BY entity_name
            LIMIT $limit
            """;

    static final String RELATES_EDGES_OPEN = RELATES_EDGES.replace("RETURN a.name AS a", "AND r.valid_to IS NULL\nRETURN a.name AS a");
    static final String RELATIONS_OPEN = RELATIONS.replace("RETURN s.display_name AS subject",
            "AND r.valid_to IS NULL\nRETURN s.display_name AS subject");

    /** Every statement by its Python name (the parity test compares them with the recorded Python texts). */
    static final Map<String, String> STATEMENTS = Map.ofEntries(
            Map.entry("constraint", CONSTRAINT), Map.entry("co_occurs_edges", CO_OCCURS_EDGES),
            Map.entry("relates_edges", RELATES_EDGES), Map.entry("existing", EXISTING), Map.entry("merge", MERGE),
            Map.entry("members", MEMBERS), Map.entry("delete", DELETE), Map.entry("display_names", DISPLAY_NAMES),
            Map.entry("relations", RELATIONS), Map.entry("set_summary", SET_SUMMARY), Map.entry("list", LIST),
            Map.entry("by_entity", BY_ENTITY), Map.entry("member_names", MEMBER_NAMES));

    private static final int MEMBER_LIMIT = 25;
    private static final int SUMMARY_LIMIT = 20000;
    private static final System.Logger LOG = System.getLogger("com.ragleap.graph");

    /** Outcome of a refresh; error is null on success. */
    public record Refresh(boolean success, int communities, int created, int kept, int removed, int summarized,
                          int summaryErrors, String error) {
    }

    /** One stored community; members (up to 25 display names) is null unless asked for. */
    public record Hit(String communityId, int size, double internalWeight, String summary, int matchedEntities,
                      List<String> members) {
    }

    private static final class State {
        int communities;
        int created;
        int kept;
        int removed;
        int summarized;
        int summaryErrors;
    }

    private record PairKey(String low, String high) {
    }

    private record Pair(String a, String b, double weight) {
    }

    private final CypherRunner runner;
    private final AuditSink audit;
    private final boolean temporal;

    CommunityStore(CypherRunner runner, AuditSink audit) {
        this(runner, audit, false);
    }

    /** With temporal=true only open RELATES_AS edges (valid_to not set) count for communities. */
    CommunityStore(CypherRunner runner, AuditSink audit, boolean temporal) {
        this.runner = runner;
        this.audit = audit == null ? AuditSink.NOOP : audit;
        this.temporal = temporal;
    }

    /** Groups the entities of a namespace into communities without writing anything. */
    public List<Communities.Community> detect(String namespace, String userId, double resolution, int minSize,
                                              boolean includeRelations, int maxEdges) {
        checkArgs(resolution, minSize, maxEdges);
        if (runner == null) {
            LOG.log(Level.WARNING, GraphIndex.NOT_AVAILABLE);
            return List.of();
        }
        String ns = namespace == null ? "" : namespace;
        String uid = userId == null ? "" : userId;
        try {
            List<Communities.Edge> edges = fetchEdges(ns, uid, includeRelations, maxEdges);
            List<Communities.Community> communities = Communities.detect(edges, resolution, minSize);
            Map<String, Object> detail = new LinkedHashMap<>();
            detail.put("edges", edges.size());
            detail.put("communities", communities.size());
            detail.put("resolution", resolution);
            audit("detect_communities", uid, ns, detail);
            return communities;
        } catch (IllegalArgumentException e) {
            throw e;
        } catch (RuntimeException e) {
            LOG.log(Level.ERROR, "Community detection error: " + e.getMessage(), e);
            return List.of();
        }
    }

    /**
     * Detects the communities and stores them. A community is identified by a hash of its members: unchanged ones are
     * kept (with their summary), changed ones are replaced, vanished ones are deleted. The summarizer gets one map
     * (community_id, members, display_names, size, internal_weight, top_pairs, relations) and returns text; null or
     * blank text, or an exception, is counted in summaryErrors and the refresh carries on.
     */
    public Refresh refresh(String namespace, String userId, double resolution, int minSize, boolean includeRelations,
                           int maxEdges, Function<Map<String, Object>, String> summarizer, boolean force,
                           int maxSummaryItems) {
        checkArgs(resolution, minSize, maxEdges);
        if (maxSummaryItems < 1) {
            throw new IllegalArgumentException("max_summary_items must be a positive integer, got " + maxSummaryItems);
        }
        State st = new State();
        if (runner == null) {
            return result(st, false, GraphIndex.NOT_AVAILABLE);
        }
        String ns = namespace == null ? "" : namespace;
        String uid = userId == null ? "" : userId;
        try {
            List<Communities.Edge> edges = fetchEdges(ns, uid, includeRelations, maxEdges);
            List<Communities.Community> found = Communities.detect(edges, resolution, minSize);
            run(CONSTRAINT);
            Map<String, Boolean> existing = new LinkedHashMap<>();
            for (Map<String, Object> row : run(EXISTING, "namespace", ns, "user_id", uid)) {
                existing.put(str(row.get("key")), row.get("has_summary") instanceof Boolean b && b);
            }
            List<String> keys = new ArrayList<>();
            for (Communities.Community c : found) {
                String key = CompositeKey.of(ns, uid, "community", c.membersHash());
                keys.add(key);
                run(MERGE, "composite_key", key, "namespace", ns, "user_id", uid, "members_hash", c.membersHash(),
                        "size", c.size(), "internal_weight", c.internalWeight(), "resolution", resolution);
                run(MEMBERS, "composite_key", key, "namespace", ns, "user_id", uid, "members", c.members());
            }
            Set<String> keep = new HashSet<>(keys);
            int created = 0;
            int kept = 0;
            for (String k : keys) {
                if (existing.containsKey(k)) {
                    kept++;
                } else {
                    created++;
                }
            }
            List<String> removed = new ArrayList<>();
            for (String k : existing.keySet()) {
                if (!keep.contains(k)) {
                    removed.add(k);
                }
            }
            removed.sort(CodePoints::compare);
            for (String key : removed) {
                run(DELETE, "composite_key", key);
            }
            st.communities = found.size();
            st.created = created;
            st.kept = kept;
            st.removed = removed.size();
            if (summarizer != null) {
                summarize(found, keys, existing, edges, ns, uid, summarizer, force, maxSummaryItems, st);
            }
            Map<String, Object> detail = new LinkedHashMap<>();
            detail.put("communities", st.communities);
            detail.put("created", st.created);
            detail.put("kept", st.kept);
            detail.put("removed", st.removed);
            detail.put("summarized", st.summarized);
            detail.put("summary_errors", st.summaryErrors);
            audit("refresh_communities", uid, ns, detail);
            return result(st, true, null);
        } catch (IllegalArgumentException e) {
            throw e;
        } catch (RuntimeException e) {
            LOG.log(Level.ERROR, "Community refresh error: " + e.getMessage(), e);
            return result(st, false, e.getMessage() == null ? "" : e.getMessage());
        }
    }

    /**
     * Stored communities. With entity names (normalized like the other lookups): the communities that contain any of
     * them, most matched entities first; with null: the largest communities.
     */
    public List<Hit> find(List<String> entityNames, String namespace, String userId, int limit, boolean includeMembers) {
        if (runner == null) {
            LOG.log(Level.WARNING, GraphIndex.NOT_AVAILABLE);
            return List.of();
        }
        List<String> names = null;
        if (entityNames != null) {
            names = EntityExtraction.normalizeEntityList(entityNames);
            if (names.isEmpty()) {
                return List.of();
            }
        }
        String ns = namespace == null ? "" : namespace;
        String uid = userId == null ? "" : userId;
        int lim = Math.max(1, limit);
        try {
            List<Map<String, Object>> rows = names == null
                    ? run(LIST, "namespace", ns, "user_id", uid, "limit", lim)
                    : run(BY_ENTITY, "namespace", ns, "user_id", uid, "entity_names", names, "limit", lim);
            List<Hit> found = new ArrayList<>();
            for (Map<String, Object> row : rows) {
                List<String> members = null;
                if (includeMembers) {
                    members = new ArrayList<>();
                    for (Map<String, Object> m : run(MEMBER_NAMES, "community_id", row.get("community_id"),
                            "user_id", uid, "limit", MEMBER_LIMIT)) {
                        members.add(str(m.get("entity_name")));
                    }
                }
                found.add(new Hit(str(row.get("community_id")), asInt(row.get("member_count")),
                        asDouble(row.get("internal_weight")), str(row.get("summary")),
                        asInt(row.get("matched_entities")), members));
            }
            Map<String, Object> detail = new LinkedHashMap<>();
            detail.put("entity_names", names);
            detail.put("result_count", found.size());
            audit("find_communities", uid, ns, detail);
            return found;
        } catch (RuntimeException e) {
            LOG.log(Level.ERROR, "Community lookup error: " + e.getMessage(), e);
            return List.of();
        }
    }

    private void summarize(List<Communities.Community> found, List<String> keys, Map<String, Boolean> existing,
                           List<Communities.Edge> edges, String ns, String uid,
                           Function<Map<String, Object>, String> summarizer, boolean force, int limit, State st) {
        Map<String, String> owner = new HashMap<>();
        for (int i = 0; i < found.size(); i++) {
            for (String name : found.get(i).members()) {
                owner.put(name, keys.get(i));
            }
        }
        Map<PairKey, Double> pairs = new LinkedHashMap<>();
        for (Communities.Edge e : edges) {
            PairKey k = CodePoints.compare(e.a(), e.b()) < 0 ? new PairKey(e.a(), e.b()) : new PairKey(e.b(), e.a());
            pairs.put(k, pairs.getOrDefault(k, 0.0) + e.weight());
        }
        Map<String, List<Pair>> byKey = new LinkedHashMap<>();
        for (Map.Entry<PairKey, Double> p : pairs.entrySet()) {
            String a = p.getKey().low();
            String b = p.getKey().high();
            if (!a.equals(b) && owner.containsKey(a) && Objects.equals(owner.get(a), owner.get(b))) {
                byKey.computeIfAbsent(owner.get(a), x -> new ArrayList<>()).add(new Pair(a, b, p.getValue()));
            }
        }
        for (int i = 0; i < found.size(); i++) {
            String key = keys.get(i);
            Communities.Community community = found.get(i);
            boolean needs = force || !existing.containsKey(key) || !existing.get(key);
            if (!needs) {
                continue;
            }
            Map<String, String> names = new HashMap<>();
            for (Map<String, Object> row : run(DISPLAY_NAMES, "namespace", ns, "user_id", uid, "members",
                    community.members())) {
                names.put(str(row.get("name")), str(row.get("display_name")));
            }
            List<Object> relations = new ArrayList<>();
            for (Map<String, Object> row : run(temporal ? RELATIONS_OPEN : RELATIONS, "namespace", ns, "user_id", uid, "members",
                    community.members(), "limit", limit)) {
                Map<String, Object> rel = new LinkedHashMap<>();
                rel.put("subject", row.get("subject"));
                rel.put("relation_type", row.get("relation_type"));
                rel.put("object", row.get("object"));
                rel.put("weight", asDouble(row.get("weight")));
                relations.add(rel);
            }
            List<Pair> sorted = new ArrayList<>(byKey.getOrDefault(key, List.of()));
            sorted.sort((x, y) -> {
                int c = Double.compare(y.weight(), x.weight());
                if (c != 0) {
                    return c;
                }
                c = CodePoints.compare(x.a(), y.a());
                return c != 0 ? c : CodePoints.compare(x.b(), y.b());
            });
            List<Object> topPairs = new ArrayList<>();
            for (Pair p : sorted.subList(0, Math.min(limit, sorted.size()))) {
                Map<String, Object> m = new LinkedHashMap<>();
                m.put("a", p.a());
                m.put("b", p.b());
                m.put("weight", p.weight());
                topPairs.add(m);
            }
            List<String> displayNames = new ArrayList<>();
            for (String member : community.members()) {
                String shown = names.get(member);
                displayNames.add(shown == null || shown.isEmpty() ? member : shown);
            }
            Map<String, Object> info = new LinkedHashMap<>();
            info.put("community_id", key);
            info.put("members", new ArrayList<>(community.members()));
            info.put("display_names", displayNames);
            info.put("size", community.size());
            info.put("internal_weight", community.internalWeight());
            info.put("top_pairs", topPairs);
            info.put("relations", relations);
            String text;
            try {
                text = summarizer.apply(info);
                if (text == null || PyText.strip(text).isEmpty()) {
                    throw new IllegalArgumentException("summarizer must return a non-empty string");
                }
            } catch (RuntimeException exc) {
                st.summaryErrors++;
                LOG.log(Level.WARNING, "Community summary failed: " + exc.getMessage());
                continue;
            }
            run(SET_SUMMARY, "composite_key", key, "summary", clip(PyText.strip(text), SUMMARY_LIMIT));
            st.summarized++;
        }
    }

    private static void checkArgs(double resolution, int minSize, int maxEdges) {
        Communities.detect(List.of(), resolution, minSize);
        if (maxEdges < 1) {
            throw new IllegalArgumentException("max_edges must be a positive integer, got " + maxEdges);
        }
    }

    private List<Communities.Edge> fetchEdges(String ns, String uid, boolean includeRelations, int maxEdges) {
        List<String> queries = includeRelations ? List.of(CO_OCCURS_EDGES, temporal ? RELATES_EDGES_OPEN : RELATES_EDGES) : List.of(CO_OCCURS_EDGES);
        Number limit = maxEdges < Integer.MAX_VALUE ? (Number) Integer.valueOf(maxEdges + 1) : Long.valueOf(maxEdges + 1L);
        List<Communities.Edge> edges = new ArrayList<>();
        for (String query : queries) {
            List<Map<String, Object>> rows = run(query, "namespace", ns, "user_id", uid, "limit", limit);
            if (rows.size() > maxEdges) {
                throw new IllegalArgumentException("more than max_edges=" + maxEdges
                        + " edges of one kind in this namespace; raise max_edges or use a smaller namespace");
            }
            for (Map<String, Object> row : rows) {
                edges.add(new Communities.Edge(str(row.get("a")), str(row.get("b")), asDouble(row.get("weight"))));
            }
        }
        return edges;
    }

    private static Refresh result(State st, boolean success, String error) {
        return new Refresh(success, st.communities, st.created, st.kept, st.removed, st.summarized,
                st.summaryErrors, error);
    }

    private List<Map<String, Object>> run(String cypher, Object... keysAndValues) {
        Map<String, Object> params = new LinkedHashMap<>();
        for (int i = 0; i < keysAndValues.length; i += 2) {
            params.put((String) keysAndValues[i], keysAndValues[i + 1]);
        }
        return runner.run(cypher, params);
    }

    private void audit(String action, String userId, String namespace, Map<String, Object> detail) {
        try {
            audit.log(userId, namespace, action, null, null, detail);
        } catch (RuntimeException e) {
            LOG.log(Level.WARNING, "Audit log write failed, continuing without it: " + e.getMessage());
        }
    }

    private static String clip(String s, int maxCodePoints) {
        if (s.codePointCount(0, s.length()) <= maxCodePoints) {
            return s;
        }
        return s.substring(0, s.offsetByCodePoints(0, maxCodePoints));
    }

    private static String str(Object o) {
        return o == null ? null : o.toString();
    }

    private static int asInt(Object o) {
        return o instanceof Number n ? n.intValue() : 0;
    }

    private static double asDouble(Object o) {
        return o instanceof Number n ? n.doubleValue() : 0.0;
    }
}
