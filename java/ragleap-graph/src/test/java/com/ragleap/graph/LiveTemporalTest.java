package com.ragleap.graph;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.time.Duration;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;

/** Temporal relation edges against a real Neo4j (own namespace, removed afterwards). Mirrors the Python live test. */
@EnabledIfEnvironmentVariable(named = "NEO4J_HTTP_URL", matches = ".+")
class LiveTemporalTest {
    private static GraphConfig config() {
        return new GraphConfig(System.getenv("NEO4J_HTTP_URL"), System.getenv().getOrDefault("NEO4J_USER", "neo4j"),
                System.getenv().getOrDefault("NEO4J_PASSWORD", ""));
    }

    @Test
    void openCloseReopenAndAsOf() {
        String ns = "ragleap_graph_java_temporal_" + UUID.randomUUID();
        String ck = "edge-" + ns;
        String ck2 = "edge2-" + ns;
        try (QueryApiRunner direct = new QueryApiRunner(config());
             GraphIndex g = new GraphIndex(config(), ExtractionConfig.regex(), AuditSink.NOOP, true)) {
            assertTrue(g.isAvailable());
            try {
                for (String name : new String[] {"acme corp", "globex corp"}) {
                    direct.run("CREATE (:Entity {composite_key: $k, name: $n, display_name: $d, namespace: $ns, user_id: ''})",
                            Map.of("k", ns + name, "n", name, "d", name.equals("acme corp") ? "Acme Corp" : "Globex Corp", "ns", ns));
                }
                Runnable support = () -> direct.run("MATCH (rw:RelationWeight {namespace: $ns}) DELETE rw", Map.of("ns", ns));
                Runnable supportOn = () -> direct.run("CREATE (:RelationWeight {composite_key: $k, namespace: $ns, document_id: 'd1', "
                        + "subject: 'acme corp', relation_type: 'PARTNERED_WITH', object: 'globex corp', user_id: '', weight: 2.0})",
                        Map.of("k", "rw-" + ns, "ns", ns));
                java.util.function.BiConsumer<String, String> refresh = (key, type) -> direct.run(GraphIndex.RECOMPUTE_RELATES_AS_TEMPORAL,
                        Map.of("namespace", ns, "subject", "acme corp", "relation_type", type, "object", "globex corp",
                                "user_id", "", "relates_as_composite_key", key));
                java.util.function.Function<String, Map<String, Object>> edge = key -> {
                    List<Map<String, Object>> rows = direct.run("MATCH ()-[r:RELATES_AS {composite_key: $k}]->() RETURN "
                            + "toString(r.valid_from) AS vf, toString(r.valid_to) AS vt, r.weight AS w", Map.of("k", key));
                    assertEquals(1, rows.size());
                    return rows.get(0);
                };
                java.util.function.Function<OffsetDateTime, List<RelationHit>> found = at -> g.findRelations("Acme Corp", null, ns,
                        null, 25, "outgoing", at);

                supportOn.run();
                refresh.accept(ck, "PARTNERED_WITH");
                Map<String, Object> e = edge.apply(ck);
                assertNotNull(e.get("vf"));
                assertNull(e.get("vt"));
                assertEquals(2.0, ((Number) e.get("w")).doubleValue());
                assertEquals(1, found.apply(null).size());
                String firstFrom = (String) e.get("vf");

                support.run();
                refresh.accept(ck, "PARTNERED_WITH");
                e = edge.apply(ck);
                assertEquals(firstFrom, e.get("vf"));
                assertNotNull(e.get("vt"));
                OffsetDateTime vf = OffsetDateTime.parse((String) e.get("vf"));
                OffsetDateTime vt = OffsetDateTime.parse((String) e.get("vt"));
                assertTrue(vt.isAfter(vf));
                assertEquals(0, found.apply(null).size());
                assertEquals(1, found.apply(vf.plus(Duration.between(vf, vt).dividedBy(2))).size());
                assertEquals(0, found.apply(vt).size());
                assertEquals(0, found.apply(vf.minusSeconds(1)).size());
                refresh.accept(ck, "PARTNERED_WITH");
                assertEquals(vt, OffsetDateTime.parse((String) edge.apply(ck).get("vt")));

                supportOn.run();
                refresh.accept(ck, "PARTNERED_WITH");
                e = edge.apply(ck);
                assertNull(e.get("vt"));
                assertTrue(!OffsetDateTime.parse((String) e.get("vf")).isBefore(vt));
                assertEquals(1, found.apply(null).size());
                assertEquals(0, found.apply(vf.plus(Duration.between(vf, vt).dividedBy(2))).size());

                support.run();
                refresh.accept(ck2, "OTHER");
                e = edge.apply(ck2);
                assertEquals(e.get("vf"), e.get("vt"));
                assertEquals(0.0, ((Number) e.get("w")).doubleValue());
                assertEquals(0, g.findRelations("Acme Corp", "OTHER", ns, null, 25, "outgoing", null).size());
                System.out.println("live temporal (java): open, close, reopen, as_of and phantom close checked");
            } finally {
                direct.run("MATCH (n) WHERE n.namespace = $ns DETACH DELETE n", Map.of("ns", ns));
            }
        }
    }
}
