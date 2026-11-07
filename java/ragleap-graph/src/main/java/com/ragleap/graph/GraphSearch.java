package com.ragleap.graph;

import java.util.List;

/** The graph side of GraphRetriever: the GraphIndex methods it calls. GraphIndex implements it; tests use fakes. */
public interface GraphSearch {
    List<String> extractQueryEntities(String query, int maxEntities, List<String> domainTerms);

    List<DocumentHit> findDocumentsByEntities(List<String> entityNames, String namespace, String userId, int limit);

    List<RelatedEntity> searchRelatedEntities(List<String> entityNames, String namespace, String userId, int maxDepth,
            int limit);

    /** Stored communities containing the entities; only called when includeCommunities is on. Default: none. */
    default List<CommunityStore.Hit> findCommunities(List<String> entityNames, String namespace, String userId,
            int limit) {
        return List.of();
    }
}
