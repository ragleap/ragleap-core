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
 * Tenant isolation against a real Neo4j: tenant A must see exactly what it would see if it were alone, even with
 * deliberate cross-tenant edges ("bait"). Own namespaces, removed afterwards.
 */
@EnabledIfEnvironmentVariable(named = "NEO4J_HTTP_URL", matches = ".+")
class LiveTenantIsolationTest {
    private static final String PREFIX = "ragleap_graph_java_tenant_";
    private static final String RUN = UUID.randomUUID().toString().substring(0, 8);
    private static final String BASE = PREFIX + RUN + "_base";
    private static final String A = PREFIX + RUN + "_a";
    private static final String B = PREFIX + RUN + "_b";
    private static final String TEXT = "Acme Corp and Globex Corp are rivals. Acme Corp works with Initech.";
    private static final String EXTRA = "Umbrella Corp and Acme Corp are partners. Umbrella Corp works with Initech.";
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

    private static void seedTenant(QueryApiRunner direct, GraphIndex graph, String ns, boolean extra) {
        graph.upsertDocument("doc-1", "Acme news", chunks(TEXT), ns);
        if (extra) {
            graph.upsertDocument("doc-2", "Umbrella news", chunks(EXTRA), ns);
        }
        direct.run("CREATE (:Entity {composite_key: $k, name: 'zeta corp', display_name: 'Zeta Corp', namespace: $ns, "
                + "user_id: '', entity_type: 'unknown'})", Map.of("k", ns + "|zeta", "ns", ns));
        if (extra) {
            direct.run("CREATE (:Entity {composite_key: $k, name: 'gamma corp', display_name: 'Gamma Corp', "
                    + "namespace: $ns, user_id: '', entity_type: 'unknown'})", Map.of("k", ns + "|gamma", "ns", ns));
        }
        String type = extra ? "SUED_BY" : "PARTNERED_WITH";
        direct.run("MATCH (a:Entity {name: 'acme corp', namespace: $ns}), (b:Entity {name: 'globex corp', namespace: $ns}) "
                + "MERGE (a)-[r:RELATES_AS {composite_key: $ck}]->(b) "
                + "SET r.relation_type = $t, r.weight = 2.0, r.namespace = $ns, r.user_id = ''",
                Map.of("ns", ns, "ck", ns + "|rel|" + type, "t", type));
    }

    private static void bait(QueryApiRunner direct) {
        Map<String, Object> p = Map.of("a", A, "b", B);
        direct.run("MATCH (d:Document {id: 'doc-1', namespace: $a}) MATCH (y:Entity {namespace: $b}) "
                + "MERGE (d)-[:CONTAINS]->(y)", p);
        direct.run("MATCH (d:Document {namespace: $b}) MATCH (x:Entity {namespace: $a}) MERGE (d)-[:CONTAINS]->(x)", p);
        direct.run("MATCH (x:Entity {name: 'acme corp', namespace: $a}) MATCH (g:Entity {name: 'gamma corp', namespace: $b}) "
                + "MATCH (z:Entity {name: 'zeta corp', namespace: $a}) "
                + "MERGE (x)-[:BAIT_LINK]->(g) MERGE (g)-[:BAIT_LINK]->(z)", p);
        direct.run("MATCH (x:Entity {namespace: $a}) MATCH (y:Entity {namespace: $b}) "
                + "MERGE (x)-[r:RELATES_AS {composite_key: 'bo|' + x.composite_key + '|' + y.composite_key}]->(y) "
                + "SET r.relation_type = 'BAIT', r.weight = 1.0, r.namespace = $a, r.user_id = ''", p);
        direct.run("MATCH (x:Entity {namespace: $a}) MATCH (y:Entity {namespace: $b}) "
                + "MERGE (x)-[r:CO_OCCURS_WITH {composite_key: 'bc|' + x.composite_key + '|' + y.composite_key}]->(y) "
                + "SET r.weight = 1.0, r.namespace = $a, r.user_id = ''", p);
    }

    private static String norm(List<?> rows) {
        List<String> s = new ArrayList<>();
        for (Object r : rows) {
            s.add(String.valueOf(r).replaceAll("[0-9a-f]{64}", "<key>"));
        }
        Collections.sort(s);
        return String.join("\n", s);
    }

    private static Map<String, String> reads(GraphIndex g, String ns) {
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        Map<String, String> out = new TreeMap<>();
        out.put("findDocumentsByEntities", norm(g.findDocumentsByEntities(List.of("Acme Corp", "Initech"), ns, null, 1000)));
        out.put("documentEntities", norm(g.documentEntities("doc-1", ns, null)));
        out.put("findEntitiesByType", norm(g.findEntitiesByType("unknown", ns, null, 1000)));
        out.put("findLineage", norm(g.findLineage("Acme Corp", "Globex Corp", null, ns, null, 1000)));
        for (int d = 1; d <= 3; d++) {
            out.put("searchRelated" + d, norm(g.searchRelatedEntities(List.of("Acme Corp"), ns, null, d, 1000)));
        }
        for (String dir : List.of("outgoing", "incoming", "both")) {
            for (String ent : List.of("Acme Corp", "Globex Corp")) {
                out.put("relations_" + dir + "_" + ent, norm(g.findRelations(ent, null, ns, null, 1000, dir)));
                out.put("relationsAsOf_" + dir + "_" + ent, norm(g.findRelations(ent, null, ns, null, 1000, dir, now)));
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

    @Test
    void tenantASeesOnlyWhatItWouldSeeAlone() {
        try (QueryApiRunner direct = new QueryApiRunner(config()); GraphIndex graph = new GraphIndex(config())) {
            wipe(direct);
            try {
                seedTenant(direct, graph, BASE, false);
                Map<String, String> base = reads(graph, BASE);
                for (String must : List.of("documentEntities", "findEntitiesByType", "findLineage", "searchRelated1",
                        "searchRelated3", "findDocumentsByEntities", "relations_outgoing_Acme Corp",
                        "relations_incoming_Globex Corp")) {
                    assertFalse(base.get(must).isEmpty(), "baseline empty, the test would pass trivially: " + must);
                }
                seedTenant(direct, graph, A, false);
                seedTenant(direct, graph, B, true);
                direct.run("MATCH (y:Entity {namespace: $b}) SET y.display_name = 'LEAKED ' + y.name", Map.of("b", B));
                bait(direct);
                Map<String, String> crowded = reads(graph, A);
                List<String> problems = problems(base, crowded);
                assertTrue(problems.isEmpty(), String.join(" || ", problems));
            } finally {
                wipe(direct);
            }
        }
    }

    @Test
    void communityLookupsIgnoreOtherTenants() {
        try (QueryApiRunner direct = new QueryApiRunner(config()); GraphIndex graph = new GraphIndex(config())) {
            wipe(direct);
            try {
                seedTenant(direct, graph, BASE, false);
                graph.refreshCommunities(BASE, null, 1.0, 2, false, 200_000, null, false, 20);
                String base = norm(graph.findCommunities(List.of("Acme Corp"), BASE, null, 1000, true)) + "||"
                        + norm(graph.findCommunities(null, BASE, null, 1000, true));
                assertFalse(base.startsWith("||"), "baseline empty, the test would pass trivially");
                seedTenant(direct, graph, A, false);
                seedTenant(direct, graph, B, true);
                graph.refreshCommunities(A, null, 1.0, 2, false, 200_000, null, false, 20);
                graph.refreshCommunities(B, null, 1.0, 2, false, 200_000, null, false, 20);
                direct.run("MATCH (y:Entity {namespace: $b}) SET y.display_name = 'LEAKED ' + y.name", Map.of("b", B));
                Map<String, Object> p = Map.of("a", A, "b", B);
                direct.run("MATCH (c:Community {namespace: $a}) MATCH (y:Entity {name: 'acme corp', namespace: $b}) "
                        + "MERGE (y)-[:IN_COMMUNITY]->(c)", p);
                direct.run("MATCH (c:Community {namespace: $b}) MATCH (x:Entity {name: 'acme corp', namespace: $a}) "
                        + "MERGE (x)-[:IN_COMMUNITY]->(c)", p);
                String crowded = norm(graph.findCommunities(List.of("Acme Corp"), A, null, 1000, true)) + "||"
                        + norm(graph.findCommunities(null, A, null, 1000, true));
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

    private static String communityRows(QueryApiRunner direct, String ns) {
        return norm(direct.run("MATCH (c:Community {namespace: $ns}) OPTIONAL MATCH (e)-[r:IN_COMMUNITY]->(c) "
                + "RETURN count(DISTINCT c) AS communities, count(r) AS memberships", Map.of("ns", ns)));
    }

    @Test
    void refreshForOneTenantLeavesOtherTenantsCommunitiesUntouched() {
        try (QueryApiRunner direct = new QueryApiRunner(config()); GraphIndex graph = new GraphIndex(config())) {
            wipe(direct);
            try {
                seedTenant(direct, graph, A, false);
                seedTenant(direct, graph, B, true);
                graph.refreshCommunities(A, null, 1.0, 2, false, 200_000, null, false, 20);
                graph.refreshCommunities(B, null, 1.0, 2, false, 200_000, null, false, 20);
                String before = communityRows(direct, B);
                assertFalse(before.contains("communities=0"), "tenant B has no communities, the test would pass trivially: " + before);
                graph.refreshCommunities(A, null, 1.0, 2, false, 200_000, null, true, 20);
                assertEquals(before, communityRows(direct, B));
            } finally {
                wipe(direct);
            }
        }
    }

    @Test
    void summarizerInputsContainOnlyTheOwnTenant() {
        try (QueryApiRunner direct = new QueryApiRunner(config()); GraphIndex graph = new GraphIndex(config())) {
            wipe(direct);
            try {
                seedTenant(direct, graph, BASE, false);
                List<Map<String, Object>> baseInfos = new ArrayList<>();
                graph.refreshCommunities(BASE, null, 1.0, 2, true, 200_000, info -> {
                    baseInfos.add(info);
                    return "summary";
                }, true, 20);
                assertFalse(baseInfos.isEmpty(), "the summarizer was never called");
                seedTenant(direct, graph, A, false);
                seedTenant(direct, graph, B, true);
                direct.run("MATCH (y:Entity {namespace: $b}) SET y.display_name = 'LEAKED ' + y.name", Map.of("b", B));
                bait(direct);
                List<Map<String, Object>> infos = new ArrayList<>();
                graph.refreshCommunities(A, null, 1.0, 2, true, 200_000, info -> {
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
