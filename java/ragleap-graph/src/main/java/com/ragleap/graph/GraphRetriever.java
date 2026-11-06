package com.ragleap.graph;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Combines vector search with entity-based graph traversal into one retrieval call (port of
 * ragleap_graph.retrieval.GraphRetriever). Does not own the GraphSearch or VectorSearch it is given.
 *
 * <p>Differences from Python: the graph must not be null (Python fails on first use); hybrid mode without a
 * VectorSearch fails at retrieve time, after the graph lookups, like Python's AttributeError; a chunk whose text is
 * present but not a string raises IllegalArgumentException (Python: TypeError).
 */
public final class GraphRetriever {
    private static final int PREVIEW_CODE_POINTS = 150;

    private final GraphSearch graph;
    private final VectorSearch vector;
    private final GraphRetrievalConfig config;

    public GraphRetriever(GraphSearch graph, VectorSearch vector) {
        this(graph, vector, GraphRetrievalConfig.defaults());
    }

    public GraphRetriever(GraphSearch graph, VectorSearch vector, GraphRetrievalConfig config) {
        if (graph == null) {
            throw new NullPointerException("graph");
        }
        this.graph = graph;
        this.vector = vector;
        this.config = config == null ? GraphRetrievalConfig.defaults() : config;
    }

    public RetrievalResult retrieve(String query) {
        return retrieve(query, 5, null, null);
    }

    public RetrievalResult retrieve(String query, int topK, String namespace, Map<String, Object> metadataFilter) {
        List<String> queryEntities = graph.extractQueryEntities(query, config.maxQueryEntities(), config.domainTerms());

        List<DocumentHit> docs;
        List<RelatedEntity> related;
        List<CommunityStore.Hit> communities = config.includeCommunities() ? List.of() : null;
        if (queryEntities == null || queryEntities.isEmpty()) {
            docs = List.of();
            related = List.of();
        } else {
            docs = graph.findDocumentsByEntities(queryEntities, namespace, null, config.maxGraphResults());
            related = graph.searchRelatedEntities(queryEntities, namespace, null, config.graphExpandDepth(),
                    config.maxGraphResults());
            if (config.includeCommunities()) {
                communities = graph.findCommunities(queryEntities, namespace, null, config.maxCommunities());
            }
        }

        List<Map<String, Object>> chunks;
        String method;
        if (config.mode() == GraphRetrievalConfig.Mode.GRAPH_ONLY) {
            chunks = List.of();
            method = "graph_only";
        } else {
            if (vector == null) {
                throw new IllegalStateException("hybrid mode needs a VectorSearch (none was given)");
            }
            chunks = vector.retrieve(query, topK, metadataFilter);
            method = "hybrid_vector_graph";
        }

        Set<Object> chunkDocumentIds = new HashSet<>();
        for (Map<String, Object> c : chunks) {
            Object id = c.get("document_id");
            if (id != null) {
                chunkDocumentIds.add(id);
            }
        }
        List<RetrievalResult.RelatedDocument> relatedDocuments = new ArrayList<>();
        for (DocumentHit h : docs) {
            relatedDocuments.add(new RetrievalResult.RelatedDocument(h,
                    h.documentId() != null && chunkDocumentIds.contains(h.documentId())));
        }

        List<RetrievalResult.Citation> citations = citations(chunks, related, communities);
        return new RetrievalResult(chunks, queryEntities, new RetrievalResult.GraphContext(relatedDocuments, related, communities),
                citations, method);
    }

    private static List<RetrievalResult.Citation> citations(List<Map<String, Object>> chunks,
            List<RelatedEntity> related, List<CommunityStore.Hit> communities) {
        List<RetrievalResult.Citation> out = new ArrayList<>();
        int number = 1;
        for (Map<String, Object> chunk : chunks) {
            Object value = chunk.containsKey("text") ? chunk.get("text") : "";
            if (!(value instanceof String text)) {
                throw new IllegalArgumentException("chunk text must be a string, got "
                        + (value == null ? "null" : value.getClass().getSimpleName()));
            }
            int codePoints = text.codePointCount(0, text.length());
            String preview = PyText.truncateCodePoints(text, PREVIEW_CODE_POINTS)
                    + (codePoints > PREVIEW_CODE_POINTS ? "..." : "");
            Object name = chunk.containsKey("document_name") ? chunk.get("document_name") : "unknown document";
            out.add(new RetrievalResult.ChunkCitation(number++, name, chunk.get("document_id"), chunk.get("chunk_id"),
                    preview));
        }
        for (RelatedEntity e : related) {
            out.add(new RetrievalResult.GraphPathCitation(e.entityName(), e.relationship(), e.depth()));
        }
        if (communities != null) {
            for (CommunityStore.Hit h : communities) {
                String summary = h.summary();
                String preview = null;
                if (summary != null) {
                    int cps = summary.codePointCount(0, summary.length());
                    preview = PyText.truncateCodePoints(summary, PREVIEW_CODE_POINTS)
                            + (cps > PREVIEW_CODE_POINTS ? "..." : "");
                }
                out.add(new RetrievalResult.CommunityCitation(h.communityId(), h.size(), preview, h.matchedEntities()));
            }
        }
        return out;
    }
}
