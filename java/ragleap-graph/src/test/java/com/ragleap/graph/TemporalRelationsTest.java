package com.ragleap.graph;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

/** Temporal relation edges on a scripted runner (exact statements against Python are in TemporalParityTest). */
class TemporalRelationsTest {
    static final class Row extends HashMap<String, Object> {
        @Override
        public Object get(Object key) {
            return "relation_type".equals(key) ? "PARTNERED_WITH" : "acme corp";
        }
    }

    static final class Capture implements CypherRunner {
        final List<String> queries = new ArrayList<>();
        final List<Map<String, Object>> params = new ArrayList<>();
        boolean armed;
        boolean rows;

        @Override
        public List<Map<String, Object>> run(String cypher, Map<String, Object> p) {
            if (!armed) {
                return List.of();
            }
            queries.add(cypher);
            params.add(p);
            return rows ? List.of(new Row()) : List.of();
        }
    }

    private static GraphIndex index(Capture c, boolean temporal) {
        GraphIndex g = new GraphIndex(c, AuditSink.NOOP, x -> { }, ExtractionConfig.regex(), temporal);
        c.armed = true;
        return g;
    }

    private static String norm(String s) {
        return s.trim().replaceAll("\\s+", " ");
    }

    private static final OffsetDateTime T = OffsetDateTime.parse("2026-03-04T10:15:00+05:30");

    @Test
    void defaultReadsAreUnchanged() {
        for (String d : new String[] {"outgoing", "incoming", "both"}) {
            Capture c = new Capture();
            index(c, false).findRelations("Acme Corp", null, "ns", null, 25, d);
            assertFalse(c.queries.get(0).contains("valid_"), d);
            assertFalse(c.params.get(0).containsKey("as_of"));
        }
    }

    @Test
    void temporalReadsSeeOnlyOpenEdgesBeforeReturn() {
        for (String d : new String[] {"outgoing", "incoming", "both"}) {
            Capture c = new Capture();
            index(c, true).findRelations("Acme Corp", null, "ns", null, 25, d);
            String q = c.queries.get(0);
            String marker = d.equals("both") ? "WITH DISTINCT" : "RETURN s.display_name";
            assertEquals(1, q.split("r.valid_to IS NULL", -1).length - 1, d);
            assertTrue(q.indexOf("r.valid_to IS NULL") < q.indexOf(marker), d);
            assertFalse(q.contains("as_of"));
        }
    }

    @Test
    void asOfFiltersTheIntervalAndSendsAnIsoStringWithSeconds() {
        for (boolean temporal : new boolean[] {false, true}) {
            Capture c = new Capture();
            index(c, temporal).findRelations("Acme Corp", null, "ns", null, 25, "outgoing", T);
            String q = norm(c.queries.get(0));
            assertTrue(q.contains("r.valid_from <= datetime($as_of)") && q.contains("r.valid_to > datetime($as_of)"));
            assertFalse(q.contains("r.valid_to IS NULL AND"));
            assertEquals("2026-03-04T10:15:00+05:30", c.params.get(0).get("as_of"));
        }
    }

    @Test
    void directionIsStillCheckedFirst() {
        assertTrue(assertThrows(IllegalArgumentException.class,
                () -> index(new Capture(), true).findRelations("Acme Corp", null, null, null, 25, "sideways", T))
                .getMessage().startsWith("direction must be"));
    }

    @Test
    void communityStoreChoosesOpenVariantsOnlyWhenTemporal() {
        for (boolean temporal : new boolean[] {false, true}) {
            Capture c = new Capture();
            c.rows = false;
            GraphIndex g = index(c, temporal);
            g.detectCommunities(null, null, 1.0, 2, true, 10);
            assertEquals(2, c.queries.size());
            assertEquals(norm(temporal ? CommunityStore.RELATES_EDGES_OPEN : CommunityStore.RELATES_EDGES), norm(c.queries.get(1)));
        }
        assertEquals(1, CommunityStore.RELATES_EDGES_OPEN.split("r.valid_to IS NULL", -1).length - 1);
        assertEquals(1, CommunityStore.RELATIONS_OPEN.split("r.valid_to IS NULL", -1).length - 1);
        assertFalse(CommunityStore.RELATES_EDGES.contains("valid_") || CommunityStore.RELATIONS.contains("valid_"));
    }

    @Test
    void upsertUsesTheTemporalRefreshOnlyWhenTemporal() {
        for (boolean temporal : new boolean[] {false, true}) {
            Capture c = new Capture();
            c.rows = true;
            GraphIndex g = index(c, temporal);
            g.upsertDocument("d1", "Doc", List.of(Map.<String, Object>of("text", "Nothing to see here.")));
            boolean hasTemporalStatement = c.queries.stream().anyMatch(
                    q -> norm(q).equals(norm(GraphIndex.RECOMPUTE_RELATES_AS_TEMPORAL)));
            boolean relatesAsDelete = c.queries.stream().anyMatch(q -> q.contains("RELATES_AS") && q.contains("DELETE r2"));
            assertEquals(temporal, hasTemporalStatement, "statements: " + c.queries.size());
            assertEquals(!temporal, relatesAsDelete);
            assertEquals(temporal, c.queries.stream().anyMatch(q -> q.contains("valid_from")));
        }
    }

    /** Triangle of co-occurrence edges; everything else answers with no rows. */
    static final class Triangle implements CypherRunner {
        final List<String> queries = new ArrayList<>();
        boolean armed;

        @Override
        public List<Map<String, Object>> run(String cypher, Map<String, Object> p) {
            if (!armed) {
                return List.of();
            }
            queries.add(cypher);
            if (cypher.contains("[r:CO_OCCURS_WITH]")) {
                return List.of(Map.<String, Object>of("a", "a1", "b", "a2", "weight", 1.0),
                        Map.<String, Object>of("a", "a2", "b", "a3", "weight", 1.0),
                        Map.<String, Object>of("a", "a1", "b", "a3", "weight", 1.0));
            }
            return List.of();
        }
    }

    @Test
    void communitySummaryRelationsUseTheOpenVariantOnlyWhenTemporal() {
        for (boolean temporal : new boolean[] {false, true}) {
            Triangle runner = new Triangle();
            GraphIndex g = new GraphIndex(runner, AuditSink.NOOP, x -> { }, ExtractionConfig.regex(), temporal);
            runner.armed = true;
            CommunityStore.Refresh r = g.refreshCommunities(null, null, 1.0, 2, false, 100, info -> "summary", false, 20);
            assertTrue(r.success(), String.valueOf(r.error()));
            String expected = norm(temporal ? CommunityStore.RELATIONS_OPEN : CommunityStore.RELATIONS);
            assertTrue(runner.queries.stream().anyMatch(q -> norm(q).equals(expected)), "temporal=" + temporal);
            String other = norm(temporal ? CommunityStore.RELATIONS : CommunityStore.RELATIONS_OPEN);
            assertFalse(runner.queries.stream().anyMatch(q -> norm(q).equals(other)), "temporal=" + temporal);
        }
    }
}
