package com.ragleap.graph;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotEquals;

import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

/** Offline layer: every tenant-scoped read sends the scope it was given with every statement it runs. */
class TenantIsolationTest {
    private interface Read {
        void call(GraphIndex g, String ns, String uid);
    }

    private static final class Recorder implements CypherRunner {
        final List<String> cyphers = new ArrayList<>();
        final List<Map<String, Object>> params = new ArrayList<>();

        @Override
        public List<Map<String, Object>> run(String cypher, Map<String, Object> p) {
            cyphers.add(cypher);
            params.add(new LinkedHashMap<>(p == null ? Map.of() : p));
            if (cypher.contains("AS community_id")) {
                Map<String, Object> row = new LinkedHashMap<>();
                row.put("community_id", "k1");
                row.put("member_count", 3);
                row.put("internal_weight", 1.0);
                row.put("summary", null);
                row.put("matched_entities", 0);
                return List.of(row);
            }
            return List.of();
        }
    }

    private static Map<String, Read> reads() {
        OffsetDateTime at = OffsetDateTime.of(2026, 1, 1, 0, 0, 0, 0, ZoneOffset.UTC);
        Map<String, Read> m = new LinkedHashMap<>();
        m.put("findDocumentsByEntities", (g, ns, uid) -> g.findDocumentsByEntities(List.of("Acme Corp"), ns, uid, 25));
        m.put("searchRelatedEntities", (g, ns, uid) -> g.searchRelatedEntities(List.of("Acme Corp"), ns, uid, 3, 10));
        for (String dir : List.of("outgoing", "incoming", "both")) {
            m.put("findRelations_" + dir, (g, ns, uid) -> g.findRelations("Acme Corp", null, ns, uid, 25, dir));
            m.put("findRelationsAsOf_" + dir, (g, ns, uid) -> g.findRelations("Acme Corp", null, ns, uid, 25, dir, at));
        }
        m.put("documentEntities", (g, ns, uid) -> g.documentEntities("doc-1", ns, uid));
        m.put("findEntitiesByType", (g, ns, uid) -> g.findEntitiesByType("ORG", ns, uid, 25));
        m.put("findLineage", (g, ns, uid) -> g.findLineage("Acme Corp", "Initech", null, ns, uid, 25));
        m.put("detectCommunities", (g, ns, uid) -> g.detectCommunities(ns, uid, 1.0, 2, false, 1000));
        m.put("findCommunitiesByEntity", (g, ns, uid) -> g.findCommunities(List.of("Acme Corp"), ns, uid, 25, true));
        m.put("findCommunitiesList", (g, ns, uid) -> g.findCommunities(null, ns, uid, 25, true));
        return m;
    }

    private static Recorder run(Read read, String ns, String uid) {
        Recorder r = new Recorder();
        GraphIndex g = new GraphIndex(r, null, s -> { });
        r.cyphers.clear();
        r.params.clear();
        read.call(g, ns, uid);
        return r;
    }

    @Test
    void everyReadSendsTheScopeItWasGiven() {
        for (Map.Entry<String, Read> e : reads().entrySet()) {
            Recorder r = run(e.getValue(), "ns_x", "u_x");
            assertFalse(r.params.isEmpty(), e.getKey() + " ran no statement");
            for (int i = 0; i < r.params.size(); i++) {
                assertEquals("ns_x", r.params.get(i).get("namespace"), e.getKey() + " statement " + i);
                assertEquals("u_x", r.params.get(i).get("user_id"), e.getKey() + " statement " + i);
            }
        }
    }

    @Test
    void twoScopesSendDifferentParameters() {
        for (Map.Entry<String, Read> e : reads().entrySet()) {
            assertNotEquals(run(e.getValue(), "ns_x", "u_x").params, run(e.getValue(), "ns_y", "u_y").params, e.getKey());
        }
    }

    @Test
    void theDefaultScopeIsEmptyStrings() {
        for (Map.Entry<String, Read> e : reads().entrySet()) {
            Recorder r = run(e.getValue(), null, null);
            assertFalse(r.params.isEmpty(), e.getKey() + " ran no statement");
            for (int i = 0; i < r.params.size(); i++) {
                assertEquals("", r.params.get(i).get("namespace"), e.getKey() + " statement " + i);
                assertEquals("", r.params.get(i).get("user_id"), e.getKey() + " statement " + i);
            }
        }
    }
}
