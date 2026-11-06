package com.ragleap.graph;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

/** Opt-in community lookup of GraphRetriever (ports of the Python tests; exact recordings are in the parity test). */
class GraphRetrieverCommunitiesTest {
    static final class G implements GraphSearch {
        final List<String> entities;
        final List<CommunityStore.Hit> hits;
        final List<Object[]> calls = new ArrayList<>();

        G(List<String> entities, List<CommunityStore.Hit> hits) {
            this.entities = entities;
            this.hits = hits;
        }

        @Override
        public List<String> extractQueryEntities(String q, int max, List<String> terms) {
            return entities;
        }

        @Override
        public List<DocumentHit> findDocumentsByEntities(List<String> n, String ns, String u, int l) {
            return List.of();
        }

        @Override
        public List<RelatedEntity> searchRelatedEntities(List<String> n, String ns, String u, int d, int l) {
            return List.of();
        }

        @Override
        public List<CommunityStore.Hit> findCommunities(List<String> n, String ns, String u, int l) {
            calls.add(new Object[] {n, ns, u, l});
            return hits;
        }
    }

    private static final VectorSearch VEC = (q, k, f) -> List.of(Map.of("chunk_id", "c1", "text", "t",
            "document_id", "d1", "document_name", "a.pdf"));

    private static List<CommunityStore.Hit> two() {
        return List.of(new CommunityStore.Hit("k1", 4, 3.0, "Acme and its partners.", 1, null),
                new CommunityStore.Hit("k2", 2, 1.0, null, 1, null));
    }

    private static GraphRetrievalConfig on() {
        return GraphRetrievalConfig.builder().includeCommunities(true).build();
    }

    @Test
    void configDefaultsAndValidation() {
        GraphRetrievalConfig c = GraphRetrievalConfig.defaults();
        assertFalse(c.includeCommunities());
        assertEquals(3, c.maxCommunities());
        assertEquals("max_communities must be >= 1", assertThrows(IllegalArgumentException.class,
                () -> GraphRetrievalConfig.builder().maxCommunities(0).build()).getMessage());
    }

    @Test
    void offByDefaultChangesNothing() {
        G g = new G(List.of("Acme Corp"), two());
        RetrievalResult r = new GraphRetriever(g, VEC).retrieve("q");
        assertTrue(g.calls.isEmpty());
        assertNull(r.graphContext().communities());
        assertTrue(r.citations().stream().noneMatch(c -> c instanceof RetrievalResult.CommunityCitation));
    }

    @Test
    void enabledCallsFindCommunitiesWithExactArguments() {
        G g = new G(List.of("Acme Corp"), two());
        new GraphRetriever(g, VEC, GraphRetrievalConfig.builder().includeCommunities(true).maxCommunities(2).build())
                .retrieve("q", 5, "ns1", null);
        assertEquals(1, g.calls.size());
        assertEquals(List.of("Acme Corp"), g.calls.get(0)[0]);
        assertEquals("ns1", g.calls.get(0)[1]);
        assertNull(g.calls.get(0)[2]);
        assertEquals(2, g.calls.get(0)[3]);
    }

    @Test
    void enabledAddsContextAndCitationsAfterChunksAndGraphPaths() {
        G g = new G(List.of("Acme Corp"), two());
        RetrievalResult r = new GraphRetriever(g, VEC, on()).retrieve("q");
        assertEquals(two(), r.graphContext().communities());
        assertEquals(2, r.citations().size() - 1);
        assertTrue(r.citations().get(0) instanceof RetrievalResult.ChunkCitation);
        assertEquals(new RetrievalResult.CommunityCitation("k1", 4, "Acme and its partners.", 1), r.citations().get(1));
        assertEquals(new RetrievalResult.CommunityCitation("k2", 2, null, 1), r.citations().get(2));
    }

    @Test
    void summaryPreviewIsCutAt150CodePoints() {
        List<CommunityStore.Hit> hits = List.of(new CommunityStore.Hit("a", 2, 1.0, "x".repeat(150), 1, null),
                new CommunityStore.Hit("b", 2, 1.0, "y".repeat(151), 1, null));
        RetrievalResult r = new GraphRetriever(new G(List.of("Acme"), hits), VEC, on()).retrieve("q");
        assertEquals("x".repeat(150), ((RetrievalResult.CommunityCitation) r.citations().get(1)).summaryPreview());
        assertEquals("y".repeat(150) + "...", ((RetrievalResult.CommunityCitation) r.citations().get(2)).summaryPreview());
    }

    @Test
    void noQueryEntitiesSkipsTheLookup() {
        G g = new G(List.of(), two());
        RetrievalResult r = new GraphRetriever(g, VEC, on()).retrieve("q");
        assertTrue(g.calls.isEmpty());
        assertEquals(List.of(), r.graphContext().communities());
    }

    @Test
    void worksInGraphOnlyModeWithoutVectorSearch() {
        G g = new G(List.of("Acme Corp"), two());
        GraphRetrievalConfig c = GraphRetrievalConfig.builder().mode("graph_only").includeCommunities(true).build();
        RetrievalResult r = new GraphRetriever(g, null, c).retrieve("q");
        assertEquals(List.of(), r.chunks());
        assertEquals(2, r.graphContext().communities().size());
        assertEquals(2, r.citations().size());
    }

    @Test
    void graphContextWithoutCommunitiesKeepsThemNull() {
        assertNull(new RetrievalResult.GraphContext(List.of(), List.of()).communities());
    }

    @Test
    void graphIndexHookLooksUpCommunitiesWithoutMemberNames() {
        List<String> statements = new ArrayList<>();
        CypherRunner runner = (cypher, params) -> {
            if (cypher.contains("count(DISTINCT e)")) {
                statements.add("by_entity");
                return List.of(Map.<String, Object>of("community_id", "k1", "member_count", 2, "internal_weight", 1.5,
                        "summary", "s", "matched_entities", 1));
            }
            if (cypher.contains("AS entity_name")) {
                statements.add("member_names");
                return List.of(Map.<String, Object>of("entity_name", "A"));
            }
            return List.of();
        };
        GraphIndex index = new GraphIndex(runner, AuditSink.NOOP, x -> { });
        List<CommunityStore.Hit> hits = ((GraphSearch) index).findCommunities(List.of("Acme Corp"), "ns", null, 3);
        assertEquals(1, hits.size());
        assertNull(hits.get(0).members());
        assertEquals(List.of("by_entity"), statements);
    }
}
