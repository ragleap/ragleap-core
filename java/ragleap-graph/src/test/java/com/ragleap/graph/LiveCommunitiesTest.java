package com.ragleap.graph;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;

/** Community methods against a real Neo4j test server, in a unique namespace that is removed afterwards. */
@EnabledIfEnvironmentVariable(named = "NEO4J_HTTP_URL", matches = ".+")
class LiveCommunitiesTest {
    private static GraphConfig config() {
        return new GraphConfig(System.getenv("NEO4J_HTTP_URL"),
                System.getenv().getOrDefault("NEO4J_USER", "neo4j"),
                System.getenv().getOrDefault("NEO4J_PASSWORD", ""));
    }

    private static String display(String lower) {
        StringBuilder sb = new StringBuilder();
        for (String w : lower.split(" ")) {
            sb.append(sb.length() > 0 ? " " : "").append(Character.toUpperCase(w.charAt(0))).append(w.substring(1));
        }
        return sb.toString();
    }

    @Test
    void communityMethodsOnASeededGraph() {
        String ns = "ragleap_graph_java_comm_" + UUID.randomUUID();
        String[][] pairs = {
            {"alpha one", "alpha two"}, {"alpha two", "alpha three"}, {"alpha one", "alpha three"},
            {"beta one", "beta two"}, {"beta two", "beta three"}, {"beta one", "beta three"},
            {"alpha three", "beta one"}};
        double[] weights = {2.0, 2.0, 2.0, 3.0, 3.0, 3.0, 0.5};
        List<Communities.Edge> expectedEdges = new ArrayList<>();
        try (QueryApiRunner direct = new QueryApiRunner(config()); GraphIndex graph = new GraphIndex(config())) {
            assertTrue(graph.isAvailable());
            try {
                for (String name : new String[] {"alpha one", "alpha two", "alpha three", "beta one", "beta two", "beta three"}) {
                    direct.run("MERGE (e:Entity {composite_key: $ck}) SET e.name = $n, e.display_name = $d, "
                            + "e.namespace = $ns, e.user_id = ''",
                            Map.of("ck", CompositeKey.of(ns, "", name), "n", name, "d", display(name), "ns", ns));
                }
                for (int i = 0; i < pairs.length; i++) {
                    direct.run("MATCH (a:Entity {name: $a, namespace: $ns}), (b:Entity {name: $b, namespace: $ns}) "
                            + "MERGE (a)-[r:CO_OCCURS_WITH {composite_key: $ck}]->(b) SET r.weight = $w, r.namespace = $ns, r.user_id = ''",
                            Map.of("a", pairs[i][0], "b", pairs[i][1], "ns", ns, "w", weights[i],
                                    "ck", CompositeKey.of(ns, "", pairs[i][0], pairs[i][1])));
                    expectedEdges.add(new Communities.Edge(pairs[i][0], pairs[i][1], weights[i]));
                }
                List<Communities.Community> expected = Communities.detect(expectedEdges);
                assertTrue(expected.size() >= 1);

                assertEquals(expected, graph.detectCommunities(ns, null, 1.0, 2, false, 1000));

                CommunityStore.Refresh first = graph.refreshCommunities(ns, null, 1.0, 2, false, 1000,
                        info -> "S " + info.get("size"), false, 20);
                assertTrue(first.success(), String.valueOf(first.error()));
                assertEquals(expected.size(), first.communities());
                assertEquals(expected.size(), first.created());
                assertEquals(0, first.kept());
                assertEquals(expected.size(), first.summarized());

                CommunityStore.Refresh second = graph.refreshCommunities(ns, null, 1.0, 2, false, 1000,
                        info -> "S " + info.get("size"), false, 20);
                assertTrue(second.success(), String.valueOf(second.error()));
                assertEquals(expected.size(), second.kept());
                assertEquals(0, second.created());
                assertEquals(0, second.removed());
                assertEquals(0, second.summarized());

                List<CommunityStore.Hit> all = graph.findCommunities(null, ns, null, 25, true);
                assertEquals(expected.size(), all.size());
                for (int i = 0; i < all.size(); i++) {
                    assertEquals(expected.get(i).size(), all.get(i).size());
                    assertEquals(expected.get(i).internalWeight(), all.get(i).internalWeight());
                    assertEquals("S " + expected.get(i).size(), all.get(i).summary());
                    assertEquals(expected.get(i).size(), all.get(i).members().size());
                }
                List<CommunityStore.Hit> byEntity = graph.findCommunities(List.of("Alpha One", "alpha two"), ns, null, 5, true);
                assertEquals(1, byEntity.size(), byEntity.toString());
                assertEquals(2, byEntity.get(0).matchedEntities());
                assertTrue(byEntity.get(0).members().contains("Alpha One"), byEntity.toString());
                assertFalse(graph.findCommunities(List.of("Alpha One"), "other_" + ns, null, 5, false).size() > 0);

                List<Map<String, Object>> types = direct.run("MATCH (c:Community {namespace: $ns}) RETURN "
                        + "valueType(c.internal_weight) AS weight_type, valueType(c.size) AS size_type, "
                        + "valueType(c.resolution) AS resolution_type LIMIT 1", Map.of("ns", ns));
                System.out.println("live communities: " + expected.size() + " communities, refresh created/kept "
                        + first.created() + "/" + second.kept() + ", stored types " + types);
            } finally {
                direct.run("MATCH (n) WHERE n.namespace = $ns DETACH DELETE n", Map.of("ns", ns));
            }
        }
    }
}
