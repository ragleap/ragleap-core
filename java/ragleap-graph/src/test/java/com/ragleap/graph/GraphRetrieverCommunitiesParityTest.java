package com.ragleap.graph;

import static com.ragleap.graph.MigrationFakes.canon;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.io.IOException;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;

/**
 * Compares GraphRetriever with include_communities and the two new config fields with the real Python code (recorded
 * by parity/gen_fixtures_retrieval_communities.py): every call in order, every result, every error.
 */
@EnabledIfEnvironmentVariable(named = "RAGLEAP_GRAPH_PARITY_DIR", matches = ".+")
class GraphRetrieverCommunitiesParityTest {
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final TypeReference<List<Map<String, Object>>> CHUNKS = new TypeReference<>() { };

    private static JsonNode load() throws IOException {
        return MAPPER.readTree(Path.of(System.getenv("RAGLEAP_GRAPH_PARITY_DIR"), "retrieval_communities_cases.json").toFile());
    }

    private static List<Object> ev(Object... items) {
        return new ArrayList<>(Arrays.asList(items));
    }

    private static String text(JsonNode n) {
        return n == null || n.isNull() ? null : n.asText();
    }

    private static List<String> strings(JsonNode n) {
        List<String> out = new ArrayList<>();
        n.forEach(e -> out.add(e.asText()));
        return out;
    }

    private static GraphRetrievalConfig.Builder builder(JsonNode k) {
        GraphRetrievalConfig.Builder b = GraphRetrievalConfig.builder();
        if (k.has("mode")) {
            b.mode(k.get("mode").isNull() ? null : k.get("mode").asText());
        }
        if (k.has("graph_expand_depth")) {
            b.graphExpandDepth(k.get("graph_expand_depth").asInt());
        }
        if (k.has("max_graph_results")) {
            b.maxGraphResults(k.get("max_graph_results").asInt());
        }
        if (k.has("max_query_entities")) {
            b.maxQueryEntities(k.get("max_query_entities").asInt());
        }
        if (k.has("include_communities")) {
            b.includeCommunities(k.get("include_communities").asBoolean());
        }
        if (k.has("max_communities")) {
            b.maxCommunities(k.get("max_communities").asInt());
        }
        return b;
    }

    static final class FakeGraph implements GraphSearch {
        private final JsonNode sc;
        private final List<Object> log;

        FakeGraph(JsonNode sc, List<Object> log) {
            this.sc = sc;
            this.log = log;
        }

        @Override
        public List<String> extractQueryEntities(String query, int maxEntities, List<String> domainTerms) {
            log.add(ev("extract", query, maxEntities, domainTerms));
            return strings(sc.get("query_entities"));
        }

        @Override
        public List<DocumentHit> findDocumentsByEntities(List<String> names, String namespace, String userId, int limit) {
            log.add(ev("find_docs", new ArrayList<>(names), namespace, limit));
            List<DocumentHit> out = new ArrayList<>();
            for (JsonNode d : sc.get("docs")) {
                out.add(new DocumentHit(text(d.get("document_id")), text(d.get("document_name")),
                        d.get("matched_entities").asInt(), d.get("graph_score").asDouble(),
                        strings(d.get("matched_entity_names"))));
            }
            return out;
        }

        @Override
        public List<RelatedEntity> searchRelatedEntities(List<String> names, String namespace, String userId,
                int maxDepth, int limit) {
            log.add(ev("search_related", new ArrayList<>(names), namespace, maxDepth, limit));
            List<RelatedEntity> out = new ArrayList<>();
            for (JsonNode e : sc.get("related")) {
                out.add(new RelatedEntity(text(e.get("entity_id")), text(e.get("entity_name")),
                        text(e.get("relationship")), e.get("depth").asInt()));
            }
            return out;
        }

        @Override
        public List<CommunityStore.Hit> findCommunities(List<String> names, String namespace, String userId, int limit) {
            log.add(ev("find_comm", new ArrayList<>(names), namespace, userId, limit));
            List<CommunityStore.Hit> out = new ArrayList<>();
            for (JsonNode c : sc.get("communities")) {
                out.add(new CommunityStore.Hit(text(c.get("community_id")), c.get("size").asInt(),
                        c.get("internal_weight").asDouble(), text(c.get("summary")), c.get("matched_entities").asInt(), null));
            }
            return out;
        }
    }

    private static Map<String, Object> toMap(RetrievalResult r) {
        Map<String, Object> res = new LinkedHashMap<>();
        res.put("chunks", r.chunks());
        res.put("query_entities", r.queryEntities());
        List<Object> docs = new ArrayList<>();
        for (RetrievalResult.RelatedDocument d : r.graphContext().relatedDocuments()) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("document_id", d.document().documentId());
            m.put("document_name", d.document().documentName());
            m.put("matched_entities", d.document().matchedEntities());
            m.put("graph_score", d.document().graphScore());
            m.put("matched_entity_names", d.document().matchedEntityNames());
            m.put("already_in_vector_results", d.alreadyInVectorResults());
            docs.add(m);
        }
        List<Object> entities = new ArrayList<>();
        for (RelatedEntity e : r.graphContext().relatedEntities()) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("entity_id", e.entityId());
            m.put("entity_name", e.entityName());
            m.put("relationship", e.relationship());
            m.put("depth", e.depth());
            entities.add(m);
        }
        Map<String, Object> gc = new LinkedHashMap<>();
        gc.put("related_documents", docs);
        gc.put("related_entities", entities);
        if (r.graphContext().communities() != null) {
            List<Object> comms = new ArrayList<>();
            for (CommunityStore.Hit h : r.graphContext().communities()) {
                Map<String, Object> m = new LinkedHashMap<>();
                m.put("community_id", h.communityId());
                m.put("size", h.size());
                m.put("internal_weight", h.internalWeight());
                m.put("summary", h.summary());
                m.put("matched_entities", h.matchedEntities());
                comms.add(m);
            }
            gc.put("communities", comms);
        }
        res.put("graph_context", gc);
        List<Object> cites = new ArrayList<>();
        for (RetrievalResult.Citation c : r.citations()) {
            Map<String, Object> m = new LinkedHashMap<>();
            switch (c) {
                case RetrievalResult.ChunkCitation cc -> {
                    m.put("type", "chunk");
                    m.put("source_number", cc.sourceNumber());
                    m.put("document_name", cc.documentName());
                    m.put("document_id", cc.documentId());
                    m.put("chunk_id", cc.chunkId());
                    m.put("text_preview", cc.textPreview());
                }
                case RetrievalResult.GraphPathCitation gp -> {
                    m.put("type", "graph_path");
                    m.put("entity_name", gp.entityName());
                    m.put("relationship", gp.relationship());
                    m.put("depth", gp.depth());
                }
                case RetrievalResult.CommunityCitation cm -> {
                    m.put("type", "community");
                    m.put("community_id", cm.communityId());
                    m.put("size", cm.size());
                    m.put("summary_preview", cm.summaryPreview());
                    m.put("matched_entities", cm.matchedEntities());
                }
            }
            cites.add(m);
        }
        res.put("citations", cites);
        res.put("retrieval_method", r.retrievalMethod());
        return res;
    }

    @Test
    void retrieveMatchesPython() throws IOException {
        JsonNode cases = load().get("retrieve");
        List<String> bad = new ArrayList<>();
        int raised = 0;
        for (JsonNode sc : cases) {
            String name = sc.get("name").asText();
            List<Object> log = new ArrayList<>();
            VectorSearch vector = sc.get("rag_none").asBoolean() ? null : (q, k, f) -> {
                log.add(ev("rag", q, k, f, List.of("metadata_filter", "top_k")));
                return MAPPER.convertValue(sc.get("chunks"), CHUNKS);
            };
            GraphRetriever retriever = new GraphRetriever(new FakeGraph(sc, log), vector, builder(sc.get("config")).build());
            JsonNode call = sc.get("call");
            int topK = call.has("top_k") ? call.get("top_k").asInt() : 5;
            String ns = call.hasNonNull("namespace") ? call.get("namespace").asText() : null;
            RetrievalResult result = null;
            String raisedName = null;
            try {
                result = retriever.retrieve(sc.get("query").asText(), topK, ns, null);
            } catch (IllegalStateException e) {
                raisedName = "AttributeError";
            }
            String expRaise = text(sc.get("raises"));
            if (expRaise != null) {
                raised++;
            }
            if (!Objects.equals(expRaise, raisedName)) {
                bad.add(name + ": raises " + expRaise + " (python) vs " + raisedName + " (java)");
            } else if (expRaise == null && !canon(sc.get("result")).equals(canon(toMap(result)))) {
                bad.add(name + ": result differs\n  python: " + canon(sc.get("result")) + "\n  java:   " + canon(toMap(result)));
            }
            if (!canon(sc.get("events")).equals(canon(log))) {
                bad.add(name + ": call log differs\n  python: " + canon(sc.get("events")) + "\n  java:   " + canon(log));
            }
        }
        System.out.println("retriever communities parity: scenarios " + cases.size() + ", python errors matched " + raised
                + ", differing " + bad.size());
        assertTrue(cases.size() > 0 && raised > 0, "nothing compared");
        assertTrue(bad.isEmpty(), String.join("\n", bad.subList(0, Math.min(3, bad.size()))));
    }

    @Test
    void configMatchesPython() throws IOException {
        JsonNode cases = load().get("config");
        List<String> bad = new ArrayList<>();
        int errors = 0;
        for (JsonNode c : cases) {
            String name = c.get("name").asText();
            GraphRetrievalConfig cfg = null;
            String message = null;
            try {
                cfg = builder(c.get("kwargs")).build();
            } catch (IllegalArgumentException e) {
                message = e.getMessage();
            }
            if (c.get("ok").asBoolean()) {
                if (cfg == null) {
                    bad.add(name + ": python accepted, java threw: " + message);
                    continue;
                }
                JsonNode f = c.get("fields");
                boolean same = f.get("mode").asText().equals(cfg.modeLabel())
                        && f.get("graph_expand_depth").asInt() == cfg.graphExpandDepth()
                        && f.get("max_graph_results").asInt() == cfg.maxGraphResults()
                        && f.get("max_query_entities").asInt() == cfg.maxQueryEntities()
                        && f.get("include_communities").asBoolean() == cfg.includeCommunities()
                        && f.get("max_communities").asInt() == cfg.maxCommunities();
                if (!same) {
                    bad.add(name + ": field mismatch " + f);
                }
            } else {
                errors++;
                if (message == null) {
                    bad.add(name + ": python raised " + c.get("error_type").asText() + ", java accepted");
                } else if (!c.get("message").asText().equals(message)) {
                    bad.add(name + ": message\n  python: " + c.get("message").asText() + "\n  java:   " + message);
                }
            }
        }
        System.out.println("retriever config parity: cases " + cases.size() + ", errors matched " + errors + ", differing " + bad.size());
        assertTrue(errors > 0, "nothing compared");
        assertTrue(bad.isEmpty(), String.join("\n", bad));
    }
}
