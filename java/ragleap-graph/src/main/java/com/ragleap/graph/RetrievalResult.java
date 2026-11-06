package com.ragleap.graph;

import java.util.List;
import java.util.Map;

/**
 * What GraphRetriever returns (Python: a dict with chunks, query_entities, graph_context, citations and
 * retrieval_method). chunks are the maps the vector side returned (empty in graph_only mode).
 */
public record RetrievalResult(List<Map<String, Object>> chunks, List<String> queryEntities, GraphContext graphContext,
        List<Citation> citations, String retrievalMethod) {

    /** Documents and entities found through the graph. */
    public record GraphContext(List<RelatedDocument> relatedDocuments, List<RelatedEntity> relatedEntities,
            List<CommunityStore.Hit> communities) {
        /** Without a community lookup (communities is null). */
        public GraphContext(List<RelatedDocument> relatedDocuments, List<RelatedEntity> relatedEntities) {
            this(relatedDocuments, relatedEntities, null);
        }
    }

    /**
     * A document found through its entities. alreadyInVectorResults says the same document also surfaced as a chunk
     * (the entry is flagged, never removed, so its graph context is kept).
     */
    public record RelatedDocument(DocumentHit document, boolean alreadyInVectorResults) {
    }

    /** A citation of either kind. */
    public sealed interface Citation permits ChunkCitation, GraphPathCitation, CommunityCitation {
    }

    /** Points at a retrieved chunk. documentName is "unknown document" only when the chunk had no such key. */
    public record ChunkCitation(int sourceNumber, Object documentName, Object documentId, Object chunkId,
            String textPreview) implements Citation {
    }

    /** Points at an entity reached by graph traversal. */
    public record GraphPathCitation(String entityName, String relationship, int depth) implements Citation {
    }

    /** Points at a stored community; summaryPreview is null until the community was summarized. */
    public record CommunityCitation(String communityId, int size, String summaryPreview, int matchedEntities)
            implements Citation {
    }
}
