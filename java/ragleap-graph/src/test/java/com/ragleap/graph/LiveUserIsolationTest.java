package com.ragleap.graph;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.TreeSet;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;

/**
 * User and namespace isolation against a real Neo4j. Tenant scope is (namespace, user_id): a user must see exactly what
 * it would see alone, even when another user shares the namespace and foreign edges (bait) point both ways.
 */
@EnabledIfEnvironmentVariable(named = "NEO4J_HTTP_URL", matches = ".+")
class LiveUserIsolationTest {
    private static final String PREFIX = "ragleap_graph_java_user_";
    private static final String RUN = UUID.randomUUID().toString().substring(0, 8);
    private static final String BASE = PREFIX + RUN + "_base";
    private static final String NS = PREFIX + RUN + "_crowd";
    private static final String OTHER = PREFIX + RUN + "_other";
    private static final String U1 = "user-one";
    private static final String U2 = "user-two";
    private static final String TEXT = "Acme Corp and Globex Corp are rivals. Acme Corp works with Initech.";
    private static final List<String> FOREIGN = List.of("leaked", "umbrella", "gamma", "bait", "sued_by");

    private static GraphConfig config() {
        return new GraphConfig(System.getenv("NEO4J_HTTP_URL"),
                System.getenv().getOrDefault("NEO4J_USER", "neo4j"),
                System.getenv().getOrDefault("NEO4J_PASSWORD", ""));
    }

    private static List<Map<String, Object>> chunks(String text) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("text", text);
        List<Map<String, Object>> list = new ArrayList<>();
        list.add(m);
        return list;
    }

    private static void wipe(QueryApiRunner direct) {
        direct.run("MATCH (n) WHERE n.namespace STARTS WITH $p DETACH DELETE n", Map.of("p", PREFIX));
    }

    private static String norm(List<?> rows) {
        List<String> s = new ArrayList<>();
        for (Object r : rows) {
            s.add(String.valueOf(r).replaceAll("[0-9a-f]{64}", "<key>"));
        }
        Collections.sort(s);
        return String.join("\n", s);
    }

    private static void seedOwn(QueryApiRunner direct, GraphIndex graph, String ns) {
        graph.upsertDocument("doc-1", "Acme news", chunks(TEXT), ns, U1, 80, 150, null);
        Map<String, Object> p = Map.of("ns", ns, "uid", U1, "k", ns + "|" + U1);
        direct.run("CREATE (:Entity {composite_key: $k + '|zeta', name: 'zeta corp', display_name: 'Zeta Corp', "
                + "namespace: $ns, user_id: $uid, entity_type: 'unknown'})", p);
        direct.run("MATCH (a:Entity {name: 'acme corp', namespace: $ns, user_id: $uid}), "
                + "(b:Entity {name: 'globex corp', namespace: $ns, user_id: $uid}) "
                + "MERGE (a)-[r:RELATES_AS {composite_key: $k + '|rel'}]->(b) "
                + "SET r.relation_type = 'PARTNERED_WITH', r.weight = 2.0, r.namespace = $ns, r.user_id = $uid", p);
        direct.run("CREATE (:RelationWeight {composite_key: $k + '|rw', namespace: $ns, user_id: $uid, "
                + "subject: 'acme corp', object: 'globex corp', relation_type: 'PARTNERED_WITH', "
                + "document_id: 'doc-1', weight: 2.0})", p);
    }

    private static void decoyEntities(QueryApiRunner direct, String ns, String uid, String tag) {
        String[][] decoys = {{"acme corp", "Leaked Acme Corp"}, {"globex corp", "Leaked Globex Corp"},
                {"gamma corp", "Gamma Corp"}, {"umbrella corp", "Umbrella Corp"}};
        for (String[] d : decoys) {
            Map<String, Object> p = new LinkedHashMap<>();
            p.put("k", ns + "|" + uid + "|" + tag + "|" + d[0]);
            p.put("name", d[0]);
            p.put("dn", d[1]);
            p.put("ns", ns);
            p.put("uid", uid);
            direct.run("CREATE (:Entity {composite_key: $k, name: $name, display_name: $dn, namespace: $ns, "
                    + "user_id: $uid, entity_type: 'unknown'})", p);
        }
    }

    private static void foreignStructure(QueryApiRunner direct) {
        Map<String, Object> p = Map.of("ns", NS, "u1", U1, "u2", U2, "other", OTHER);
        direct.run("CREATE (:Document {composite_key: $ns + '|' + $u2 + '|doc-1', id: 'doc-1', title: 'Gamma news', "
                + "namespace: $ns, user_id: $u2})", p);
        direct.run("MATCH (d:Document {id: 'doc-1', namespace: $ns, user_id: $u2}) "
                + "MATCH (y:Entity {namespace: $ns, user_id: $u2}) MERGE (d)-[:CONTAINS]->(y)", p);
        direct.run("MATCH (x:Entity {name: 'acme corp', namespace: $ns, user_id: $u2}) "
                + "MATCH (y:Entity {name: 'globex corp', namespace: $ns, user_id: $u2}) "
                + "MERGE (x)-[r:RELATES_AS {composite_key: x.composite_key + '|' + y.composite_key + '|sued'}]->(y) "
                + "SET r.relation_type = 'SUED_BY', r.weight = 3.0, r.namespace = $ns, r.user_id = $u2", p);
        direct.run("MATCH (x:Entity {namespace: $ns, user_id: $u2}) MATCH (y:Entity {namespace: $ns, user_id: $u2}) "
                + "WHERE x.composite_key < y.composite_key "
                + "MERGE (x)-[r:CO_OCCURS_WITH {composite_key: 'cu|' + x.composite_key + '|' + y.composite_key}]->(y) "
                + "SET r.weight = 3.0, r.namespace = $ns, r.user_id = $u2", p);
        direct.run("CREATE (:PairWeight {composite_key: $ns + '|' + $u2 + '|pw', namespace: $ns, user_id: $u2, "
                + "entity_a: 'acme corp', entity_b: 'globex corp', document_id: 'gamma-doc', weight: 5.0})", p);
        direct.run("CREATE (:RelationWeight {composite_key: $ns + '|' + $u2 + '|rw', namespace: $ns, user_id: $u2, "
                + "subject: 'acme corp', object: 'globex corp', relation_type: 'SUED_BY', "
                + "document_id: 'gamma-doc', weight: 5.0})", p);
        direct.run("CREATE (:RelationWeight {composite_key: $other + '|' + $u1 + '|rw', namespace: $other, "
                + "user_id: $u1, subject: 'acme corp', object: 'globex corp', relation_type: 'SUED_BY', "
                + "document_id: 'gamma-doc', weight: 5.0})", p);
    }

    private static void crossBait(QueryApiRunner direct) {
        Map<String, Object> p = Map.of("ns", NS, "u1", U1, "u2", U2, "other", OTHER);
        direct.run("MATCH (x:Entity {namespace: $ns, user_id: $u1}) MATCH (y:Entity {namespace: $ns, user_id: $u2}) "
                + "MERGE (x)-[r:RELATES_AS {composite_key: 'bo|' + x.composite_key + '|' + y.composite_key}]->(y) "
                + "SET r.relation_type = 'BAIT', r.weight = 1.0, r.namespace = $ns, r.user_id = $u1 "
                + "MERGE (y)-[q:RELATES_AS {composite_key: 'bi|' + x.composite_key + '|' + y.composite_key}]->(x) "
                + "SET q.relation_type = 'BAIT', q.weight = 1.0, q.namespace = $ns, q.user_id = $u2", p);
        direct.run("MATCH (x:Entity {namespace: $ns, user_id: $u1}) MATCH (z:Entity {namespace: $other, user_id: $u1}) "
                + "MERGE (x)-[r:RELATES_AS {composite_key: 'oo|' + x.composite_key + '|' + z.composite_key}]->(z) "
                + "SET r.relation_type = 'BAIT', r.weight = 1.0, r.namespace = $ns, r.user_id = $u1 "
                + "MERGE (z)-[q:RELATES_AS {composite_key: 'oi|' + x.composite_key + '|' + z.composite_key}]->(x) "
                + "SET q.relation_type = 'BAIT', q.weight = 1.0, q.namespace = $other, q.user_id = $u1", p);
        direct.run("MATCH (x:Entity {namespace: $ns, user_id: $u1}) MATCH (y:Entity {namespace: $ns, user_id: $u2}) "
                + "WHERE y.name IN ['gamma corp', 'umbrella corp'] "
                + "MERGE (x)-[r:CO_OCCURS_WITH {composite_key: 'bc|' + x.composite_key + '|' + y.composite_key}]->(y) "
                + "SET r.weight = 5.0, r.namespace = $ns, r.user_id = $u1", p);
        direct.run("MATCH (d:Document {id: 'doc-1', namespace: $ns, user_id: $u1}) "
                + "MATCH (y:Entity {namespace: $ns, user_id: $u2}) MERGE (d)-[:CONTAINS]->(y)", p);
        direct.run("MATCH (d:Document {id: 'doc-1', namespace: $ns, user_id: $u2}) "
                + "MATCH (x:Entity {namespace: $ns, user_id: $u1}) MERGE (d)-[:CONTAINS]->(x)", p);
    }

    private static void assertBaitExists(QueryApiRunner direct) {
        Map<String, Object> p = Map.of("ns", NS, "u1", U1, "u2", U2, "other", OTHER);
        String s = norm(direct.run("OPTIONAL MATCH (:Entity {namespace: $ns, user_id: $u2})-[r1:RELATES_AS "
                + "{relation_type: 'BAIT'}]->(:Entity {namespace: $ns, user_id: $u1}) WITH count(r1) AS a "
                + "OPTIONAL MATCH (:Entity {namespace: $other, user_id: $u1})-[r2:RELATES_AS {relation_type: 'BAIT'}]->"
                + "(:Entity {namespace: $ns, user_id: $u1}) WITH a, count(r2) AS b "
                + "OPTIONAL MATCH (:Document {namespace: $ns, user_id: $u2})-[r3:CONTAINS]->"
                + "(:Entity {namespace: $ns, user_id: $u1}) WITH a, b, count(r3) AS c "
                + "OPTIONAL MATCH (:Entity {namespace: $ns, user_id: $u1})-[r4:CO_OCCURS_WITH]->"
                + "(:Entity {namespace: $ns, user_id: $u2}) RETURN a, b, c, count(r4) AS d", p));
        assertFalse(s.contains("=0"), "the bait was not created, the test would pass trivially: " + s);
    }

    private static void buildCrowd(QueryApiRunner direct, GraphIndex graph, boolean otherUserCommunities) {
        decoyEntities(direct, NS, U2, "pre");
        decoyEntities(direct, OTHER, U1, "pre");
        seedOwn(direct, graph, NS);
        decoyEntities(direct, NS, U2, "post");
        decoyEntities(direct, OTHER, U1, "post");
        foreignStructure(direct);
        if (otherUserCommunities) {
            graph.refreshCommunities(NS, U2, 1.0, 2, false, 200_000, null, false, 20);
        }
        crossBait(direct);
        assertBaitExists(direct);
    }

    private static Map<String, String> reads(GraphIndex g, String ns) {
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        Map<String, String> out = new TreeMap<>();
        out.put("findDocumentsByEntities", norm(g.findDocumentsByEntities(List.of("Acme Corp", "Initech"), ns, U1, 1000)));
        out.put("documentEntities", norm(g.documentEntities("doc-1", ns, U1)));
        out.put("findEntitiesByType", norm(g.findEntitiesByType("unknown", ns, U1, 1000)));
        out.put("findLineage", norm(g.findLineage("Acme Corp", "Globex Corp", null, ns, U1, 1000)));
        for (int d = 1; d <= 3; d++) {
            out.put("searchRelated" + d, norm(g.searchRelatedEntities(List.of("Acme Corp"), ns, U1, d, 1000)));
        }
        for (String dir : List.of("outgoing", "incoming", "both")) {
            for (String ent : List.of("Acme Corp", "Globex Corp")) {
                out.put("relations_" + dir + "_" + ent, norm(g.findRelations(ent, null, ns, U1, 1000, dir)));
                out.put("relationsAsOf_" + dir + "_" + ent, norm(g.findRelations(ent, null, ns, U1, 1000, dir, now)));
            }
        }
        return out;
    }

    private static List<String> problems(Map<String, String> base, Map<String, String> crowded) {
        List<String> out = new ArrayList<>();
        for (String k : base.keySet()) {
            String b = base.get(k);
            String c = crowded.get(k);
            if (!b.equals(c)) {
                Set<String> extra = new TreeSet<>(Arrays.asList(c.split("\n")));
                extra.removeAll(Arrays.asList(b.split("\n")));
                out.add(k + " differs, only in the crowded read: " + extra);
            }
            String low = c.toLowerCase();
            for (String w : FOREIGN) {
                if (low.contains(w)) {
                    out.add(k + " contains the foreign word '" + w + "'");
                    break;
                }
            }
        }
        return out;
    }

    private static String communityReads(GraphIndex g, String ns) {
        return norm(g.findCommunities(List.of("Acme Corp"), ns, U1, 1000, true)) + "||"
                + norm(g.findCommunities(null, ns, U1, 1000, true));
    }

    private static String communityCount(QueryApiRunner direct, String ns, String uid) {
        return norm(direct.run("MATCH (c:Community {namespace: $ns, user_id: $uid}) RETURN count(c) AS communities",
                Map.of("ns", ns, "uid", uid)));
    }

    @Test
    void userScopedReadsMatchWhatTheUserSeesAlone() {
        try (QueryApiRunner direct = new QueryApiRunner(config()); GraphIndex graph = new GraphIndex(config())) {
            wipe(direct);
            try {
                seedOwn(direct, graph, BASE);
                Map<String, String> base = reads(graph, BASE);
                for (String must : List.of("documentEntities", "findEntitiesByType", "findLineage", "searchRelated1",
                        "searchRelated3", "findDocumentsByEntities", "relations_outgoing_Acme Corp",
                        "relations_incoming_Globex Corp")) {
                    assertFalse(base.get(must).isEmpty(), "baseline empty, the test would pass trivially: " + must);
                }
                buildCrowd(direct, graph, false);
                List<String> problems = problems(base, reads(graph, NS));
                assertTrue(problems.isEmpty(), String.join(" || ", problems));
            } finally {
                wipe(direct);
            }
        }
    }

    @Test
    void communityLookupsIgnoreOtherUsersAndNamespaces() {
        try (QueryApiRunner direct = new QueryApiRunner(config()); GraphIndex graph = new GraphIndex(config())) {
            wipe(direct);
            try {
                seedOwn(direct, graph, BASE);
                graph.refreshCommunities(BASE, U1, 1.0, 2, true, 200_000, null, false, 20);
                String base = communityReads(graph, BASE);
                assertFalse(base.startsWith("||"), "baseline empty, the test would pass trivially");
                buildCrowd(direct, graph, true);
                graph.refreshCommunities(NS, U1, 1.0, 2, true, 200_000, null, false, 20);
                Map<String, Object> p = Map.of("ns", NS, "u1", U1, "u2", U2);
                direct.run("MATCH (c:Community {namespace: $ns, user_id: $u1}) "
                        + "MATCH (y:Entity {name: 'acme corp', namespace: $ns, user_id: $u2}) "
                        + "MERGE (y)-[:IN_COMMUNITY]->(c)", p);
                direct.run("MATCH (c:Community {namespace: $ns, user_id: $u2}) "
                        + "MATCH (x:Entity {name: 'acme corp', namespace: $ns, user_id: $u1}) "
                        + "MERGE (x)-[:IN_COMMUNITY]->(c)", p);
                String links = norm(direct.run("OPTIONAL MATCH (:Entity {namespace: $ns, user_id: $u2})-[r1:IN_COMMUNITY]->"
                        + "(:Community {namespace: $ns, user_id: $u1}) WITH count(r1) AS a "
                        + "OPTIONAL MATCH (:Entity {namespace: $ns, user_id: $u1})-[r2:IN_COMMUNITY]->"
                        + "(:Community {namespace: $ns, user_id: $u2}) RETURN a, count(r2) AS b", p));
                assertFalse(links.contains("=0"), "the community bait was not created: " + links);
                String crowded = communityReads(graph, NS);
                String low = crowded.toLowerCase();
                for (String w : FOREIGN) {
                    assertFalse(low.contains(w), "the community read contains the foreign word '" + w + "': " + crowded);
                }
                assertEquals(base, crowded);
            } finally {
                wipe(direct);
            }
        }
    }

    @Test
    void pruningRefreshKeepsOtherUsersCommunities() {
        try (QueryApiRunner direct = new QueryApiRunner(config()); GraphIndex graph = new GraphIndex(config())) {
            wipe(direct);
            try {
                seedOwn(direct, graph, NS);
                decoyEntities(direct, NS, U2, "pre");
                foreignStructure(direct);
                graph.refreshCommunities(NS, U2, 1.0, 2, false, 200_000, null, false, 20);
                String before = communityCount(direct, NS, U2);
                assertFalse(before.contains("communities=0"), "user two has no communities, the test would pass trivially: " + before);
                CommunityStore.Refresh r = graph.refreshCommunities(NS, U1, 1.0, 2, false, 200_000, null, true, 20);
                assertEquals(0, r.removed());
                assertEquals(before, communityCount(direct, NS, U2));
            } finally {
                wipe(direct);
            }
        }
    }

    @Test
    void displayNamesStatementReturnsOnlyTheOwnUsersEntities() {
        try (QueryApiRunner direct = new QueryApiRunner(config()); GraphIndex graph = new GraphIndex(config())) {
            wipe(direct);
            try {
                seedOwn(direct, graph, BASE);
                List<String> members = List.of("acme corp", "globex corp", "initech", "zeta corp");
                Map<String, Object> baseParams = Map.of("namespace", BASE, "user_id", U1, "members", members);
                List<?> baseRows = direct.run(CommunityStore.DISPLAY_NAMES, baseParams);
                assertFalse(baseRows.isEmpty(), "baseline empty, the test would pass trivially");
                buildCrowd(direct, graph, false);
                Map<String, Object> crowdParams = Map.of("namespace", NS, "user_id", U1, "members", members);
                List<?> crowdRows = direct.run(CommunityStore.DISPLAY_NAMES, crowdParams);
                String low = String.valueOf(crowdRows).toLowerCase();
                for (String w : FOREIGN) {
                    assertFalse(low.contains(w), "the display name read contains the foreign word '" + w + "': " + crowdRows);
                }
                assertEquals(norm(baseRows), norm(crowdRows));
            } finally {
                wipe(direct);
            }
        }
    }

    @Test
    void summarizerInputsIgnoreOtherUsersAndNamespaces() {
        try (QueryApiRunner direct = new QueryApiRunner(config()); GraphIndex graph = new GraphIndex(config())) {
            wipe(direct);
            try {
                seedOwn(direct, graph, BASE);
                List<Map<String, Object>> baseInfos = new ArrayList<>();
                graph.refreshCommunities(BASE, U1, 1.0, 2, true, 200_000, info -> {
                    baseInfos.add(info);
                    return "summary";
                }, true, 20);
                assertFalse(baseInfos.isEmpty(), "the summarizer was never called");
                buildCrowd(direct, graph, false);
                List<Map<String, Object>> infos = new ArrayList<>();
                graph.refreshCommunities(NS, U1, 1.0, 2, true, 200_000, info -> {
                    infos.add(info);
                    return "summary";
                }, true, 20);
                String low = String.valueOf(infos).toLowerCase();
                for (String w : FOREIGN) {
                    assertFalse(low.contains(w), "the summarizer input contains the foreign word '" + w + "': " + infos);
                }
                assertEquals(norm(baseInfos), norm(infos));
            } finally {
                wipe(direct);
            }
        }
    }
}
