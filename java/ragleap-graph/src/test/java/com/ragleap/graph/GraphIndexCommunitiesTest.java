package com.ragleap.graph;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

/** Community methods of GraphIndex against a scripted runner (exact Cypher and values are in the parity test). */
class GraphIndexCommunitiesTest {
    private static Map<String, Object> row(Object... kv) {
        Map<String, Object> m = new LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) {
            m.put((String) kv[i], kv[i + 1]);
        }
        return m;
    }

    private static final List<Map<String, Object>> TRIANGLE = List.of(
            row("a", "a1", "b", "a2", "weight", 1.0), row("a", "a2", "b", "a3", "weight", 1.0),
            row("a", "a1", "b", "a3", "weight", 1.0));

    private static final class Scripted implements CypherRunner {
        final List<Map<String, Object>> edges;
        final List<Map<String, Object>> existing;
        final List<String> names = new ArrayList<>();
        final List<Map<String, Object>> params = new ArrayList<>();
        boolean armed;

        Scripted(List<Map<String, Object>> edges, List<Map<String, Object>> existing) {
            this.edges = edges;
            this.existing = existing;
        }

        @Override
        public List<Map<String, Object>> run(String cypher, Map<String, Object> p) {
            if (!armed) {
                return List.of();
            }
            String n = cypher.replaceAll("\\s+", " ");
            String name;
            List<Map<String, Object>> out = List.of();
            if (n.contains("[r:CO_OCCURS_WITH]")) {
                name = "edges";
                out = edges;
            } else if (n.contains("AS has_summary")) {
                name = "existing";
                out = existing;
            } else if (n.contains("MERGE (c:Community")) {
                name = "merge";
            } else if (n.contains("DETACH DELETE")) {
                name = "delete";
            } else if (n.contains("e.display_name AS display_name")) {
                name = "display";
                out = List.of(row("name", "a1", "display_name", "A1"));
            } else if (n.contains("SET c.summary")) {
                name = "set_summary";
            } else {
                name = "other";
            }
            names.add(name);
            params.add(p);
            return out;
        }
    }

    private static GraphIndex index(Scripted s) {
        GraphIndex g = new GraphIndex(s, AuditSink.NOOP, x -> { });
        s.armed = true;
        return g;
    }

    private static Map<String, Object> paramsOf(Scripted s, String name) {
        for (int i = 0; i < s.names.size(); i++) {
            if (s.names.get(i).equals(name)) {
                return s.params.get(i);
            }
        }
        throw new AssertionError("no " + name + " statement");
    }

    @Test
    void unavailableIndexReturnsEmptyAndReportsError() {
        GraphIndex g = new GraphIndex((CypherRunner) null, AuditSink.NOOP, x -> { });
        assertTrue(g.detectCommunities().isEmpty());
        CommunityStore.Refresh r = g.refreshCommunities();
        assertFalse(r.success());
        assertEquals("Neo4j driver not available", r.error());
        assertTrue(g.findCommunities(null, null, null, 25, false).isEmpty());
    }

    @Test
    void argumentsAreValidatedBeforeAvailability() {
        GraphIndex g = new GraphIndex((CypherRunner) null, AuditSink.NOOP, x -> { });
        assertTrue(assertThrows(IllegalArgumentException.class,
                () -> g.detectCommunities(null, null, 0.0, 2, false, 10)).getMessage().startsWith("resolution must be"));
        assertEquals("max_edges must be a positive integer, got 0", assertThrows(IllegalArgumentException.class,
                () -> g.detectCommunities(null, null, 1.0, 2, false, 0)).getMessage());
        assertEquals("max_summary_items must be a positive integer, got 0", assertThrows(IllegalArgumentException.class,
                () -> g.refreshCommunities(null, null, 1.0, 2, false, 10, null, false, 0)).getMessage());
    }

    @Test
    void edgeLimitIsMaxEdgesPlusOneAndTooManyEdgesRaise() {
        List<Map<String, Object>> three = TRIANGLE;
        Scripted s = new Scripted(three, List.of());
        GraphIndex g = index(s);
        IllegalArgumentException ex = assertThrows(IllegalArgumentException.class,
                () -> g.detectCommunities(null, null, 1.0, 2, false, 2));
        assertTrue(ex.getMessage().startsWith("more than max_edges=2 edges of one kind"));
        s.names.clear();
        s.params.clear();
        assertEquals(1, g.detectCommunities(null, null, 1.0, 2, false, 3).size());
        assertEquals(Integer.valueOf(4), paramsOf(s, "edges").get("limit"));
    }

    @Test
    void refreshCreatesKeepsAndRemoves() {
        String hash = Communities.membersHash(List.of("a1", "a2", "a3"));
        List<Map<String, Object>> existing = List.of(
                row("key", CompositeKey.of("", "", "community", hash), "has_summary", true),
                row("key", "stale", "has_summary", false));
        Scripted s = new Scripted(TRIANGLE, existing);
        CommunityStore.Refresh r = index(s).refreshCommunities();
        assertTrue(r.success());
        assertEquals(1, r.communities());
        assertEquals(0, r.created());
        assertEquals(1, r.kept());
        assertEquals(1, r.removed());
        assertEquals("stale", paramsOf(s, "delete").get("composite_key"));
        Map<String, Object> merge = paramsOf(s, "merge");
        assertEquals(Integer.valueOf(3), merge.get("size"));
        assertEquals(Double.valueOf(3.0), merge.get("internal_weight"));
        assertEquals(Double.valueOf(1.0), merge.get("resolution"));
    }

    @Test
    void blankSummaryIsCountedAsErrorAndGoodSummaryIsStrippedAndStored() {
        Scripted s = new Scripted(TRIANGLE, List.of());
        CommunityStore.Refresh bad = index(s).refreshCommunities(null, null, 1.0, 2, false, 100, info -> "  ", false, 20);
        assertTrue(bad.success());
        assertEquals(1, bad.summaryErrors());
        assertEquals(0, bad.summarized());
        assertFalse(s.names.contains("set_summary"));
        Scripted s2 = new Scripted(TRIANGLE, List.of());
        CommunityStore.Refresh ok = index(s2).refreshCommunities(null, null, 1.0, 2, false, 100,
                info -> "  hello  ", false, 20);
        assertEquals(1, ok.summarized());
        assertEquals("hello", paramsOf(s2, "set_summary").get("summary"));
    }

    @Test
    void summaryIsCutByCodePointsAndFailingSummarizerNeverRaises() {
        String emoji = new String(Character.toChars(0x1F600));
        Scripted s = new Scripted(TRIANGLE, List.of());
        index(s).refreshCommunities(null, null, 1.0, 2, false, 100, info -> emoji.repeat(20001), false, 20);
        String stored = (String) paramsOf(s, "set_summary").get("summary");
        assertEquals(20000, stored.codePointCount(0, stored.length()));
        Scripted s2 = new Scripted(TRIANGLE, List.of());
        CommunityStore.Refresh r = index(s2).refreshCommunities(null, null, 1.0, 2, false, 100, info -> {
            throw new IllegalStateException("nope");
        }, false, 20);
        assertTrue(r.success());
        assertEquals(1, r.summaryErrors());
    }

    @Test
    void findWithOnlyBlankNamesDoesNotQuery() {
        Scripted s = new Scripted(List.of(), List.of());
        GraphIndex g = index(s);
        assertTrue(g.findCommunities(List.of("  ", "ab"), null, null, 25, false).isEmpty());
        assertTrue(g.findCommunities(List.of(), null, null, 25, false).isEmpty());
        assertTrue(s.names.isEmpty());
    }
}
