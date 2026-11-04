"""
ragleap_graph.retrieval

v0.3.0 addition: GraphRetriever fuses ragleap-rag's vector search with
ragleap-graph's entity-based graph traversal into a single retrieval
call, with graph-path citations alongside chunk citations.

Two modes:
  - "hybrid" (default): vector search for chunks + graph traversal from
    query entities, combined into one result.
  - "graph_only": skip vector search entirely, pure entity-based +
    traversal retrieval (e.g. "show all documents related to Customer X").

Design constraints (locked, matches the reasoning behind extraction.py):
  - Built on RagLeap.retrieve() (ragleap-rag >=0.12.0), not a hand-rolled
    vector-search call - reuses the real, tested embed->search->rerank
    pipeline rather than duplicating it.
  - Built on GraphIndex's existing, tested methods
    (extract_query_entities, find_documents_by_entities,
    search_related_entities) - no new Cypher queries written here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional


RetrievalMode = Literal["hybrid", "graph_only"]


@dataclass
class GraphRetrievalConfig:
    """Configuration for GraphRetriever.

    Parameters
    ----------
    mode:
        "hybrid" (default) - vector search + graph expansion combined.
        "graph_only" - skip vector search, pure entity/graph retrieval.
    graph_expand_depth:
        Passed to GraphIndex.search_related_entities()'s max_depth.
        Bounded 1-10 there already; not re-validated here.
    max_graph_results:
        Passed to both find_documents_by_entities() and
        search_related_entities()'s limit.
    domain_terms:
        Optional caller-supplied vocabulary passed through to
        GraphIndex.extract_query_entities() - same convention as the
        rest of ragleap-graph, no hardcoded vocabulary.
    max_query_entities:
        Passed to extract_query_entities()'s max_entities.
    include_communities:
        Opt-in (default False). When True, retrieve() also looks up the stored
        communities (GraphIndex.find_communities) that contain the query entities
        and adds them to graph_context["communities"] and to the citations.
        Needs communities created with GraphIndex.refresh_communities().
    max_communities:
        Passed to find_communities()'s limit. Only used when
        include_communities is True.
    """

    mode: RetrievalMode = "hybrid"
    graph_expand_depth: int = 2
    max_graph_results: int = 10
    domain_terms: Optional[list[str]] = None
    max_query_entities: int = 10
    include_communities: bool = False
    max_communities: int = 3

    def __post_init__(self) -> None:
        if self.mode not in ("hybrid", "graph_only"):
            raise ValueError(f"mode must be 'hybrid' or 'graph_only', got {self.mode!r}")
        if self.graph_expand_depth < 1:
            raise ValueError("graph_expand_depth must be >= 1")
        if self.max_graph_results < 1:
            raise ValueError("max_graph_results must be >= 1")
        if self.max_query_entities < 1:
            raise ValueError("max_query_entities must be >= 1")
        if not isinstance(self.include_communities, bool):
            raise ValueError("include_communities must be a bool")
        if self.max_communities < 1:
            raise ValueError("max_communities must be >= 1")


class GraphRetriever:
    """
    Combines vector search (via an existing RagLeap instance) with
    entity-based graph traversal (via an existing GraphIndex instance)
    into one retrieval call.

    Does not own either RagLeap or GraphIndex's lifecycle - pass in
    instances you've already constructed and configured (schema
    initialized, connections open), same pattern as passing an existing
    driver/service into a wrapper rather than this class reaching into
    construction details it shouldn't own.
    """

    def __init__(
        self,
        graph: Any,
        rag: Any,
        config: Optional[GraphRetrievalConfig] = None,
    ) -> None:
        self._graph = graph
        self._rag = rag
        self._config = config or GraphRetrievalConfig()

    def retrieve(
        self,
        query: str,
        top_k: int = 5,
        namespace: Optional[str] = None,
        metadata_filter: Optional[dict] = None,
    ) -> dict:
        """
        Retrieve for a query using the configured mode.

        Returns:
        {
            "chunks": [...],                # from rag.retrieve(), [] in graph_only mode
            "query_entities": [...],        # entity names extracted from the query itself
            "graph_context": {
                "related_documents": [...], # from GraphIndex.find_documents_by_entities()
                "related_entities": [...],  # from GraphIndex.search_related_entities()
                # include_communities=True adds "communities": [...] here
            },
            "citations": [...],             # chunk citations + graph-path citations
            "retrieval_method": "hybrid_vector_graph" | "graph_only",
        }
        """
        query_entities = self._graph.extract_query_entities(
            query,
            max_entities=self._config.max_query_entities,
            domain_terms=self._config.domain_terms,
        )

        graph_context = self._build_graph_context(query_entities, namespace)

        if self._config.mode == "graph_only":
            chunks: list[dict] = []
            retrieval_method = "graph_only"
        else:
            chunks = self._rag.retrieve(query, top_k=top_k, metadata_filter=metadata_filter)
            retrieval_method = "hybrid_vector_graph"

        # Flag, don't remove: a document can genuinely surface through
        # both vector search (as a chunk) and graph traversal (as a
        # related document) - that overlap is real signal, not noise.
        # Removing the graph-side entry would silently discard its
        # matched-entity/graph-score context. Flagging lets the caller
        # decide whether to filter, de-emphasize, or just note it.
        chunk_document_ids = {
            c.get("document_id") for c in chunks if c.get("document_id") is not None
        }
        for doc in graph_context["related_documents"]:
            doc["already_in_vector_results"] = doc.get("document_id") in chunk_document_ids

        citations = self._build_citations(chunks, graph_context)

        return {
            "chunks": chunks,
            "query_entities": query_entities,
            "graph_context": graph_context,
            "citations": citations,
            "retrieval_method": retrieval_method,
        }

    def _build_graph_context(
        self, query_entities: list[str], namespace: Optional[str]
    ) -> dict:
        context: dict = {"related_documents": [], "related_entities": []}
        if self._config.include_communities:
            context["communities"] = []
        if not query_entities:
            return context

        related_documents = self._graph.find_documents_by_entities(
            query_entities, namespace=namespace, limit=self._config.max_graph_results
        )
        related_entities = self._graph.search_related_entities(
            query_entities,
            namespace=namespace,
            max_depth=self._config.graph_expand_depth,
            limit=self._config.max_graph_results,
        )
        context["related_documents"] = related_documents
        context["related_entities"] = related_entities
        if self._config.include_communities:
            context["communities"] = self._graph.find_communities(
                query_entities,
                namespace=namespace,
                limit=self._config.max_communities,
            )
        return context

    def _build_citations(self, chunks: list[dict], graph_context: dict) -> list[dict]:
        citations: list[dict] = []

        for i, chunk in enumerate(chunks, start=1):
            text = chunk.get("text", "")
            citations.append({
                "type": "chunk",
                "source_number": i,
                "document_name": chunk.get("document_name", "unknown document"),
                "document_id": chunk.get("document_id"),
                "chunk_id": chunk.get("chunk_id"),
                "text_preview": text[:150] + ("..." if len(text) > 150 else ""),
            })

        for entity in graph_context.get("related_entities", []):
            citations.append({
                "type": "graph_path",
                "entity_name": entity.get("entity_name"),
                "relationship": entity.get("relationship"),
                "depth": entity.get("depth"),
            })

        for community in graph_context.get("communities", []):
            summary = community.get("summary")
            if summary is None:
                summary_preview = None
            else:
                summary_preview = summary[:150] + ("..." if len(summary) > 150 else "")
            citations.append({
                "type": "community",
                "community_id": community.get("community_id"),
                "size": community.get("size"),
                "summary_preview": summary_preview,
                "matched_entities": community.get("matched_entities"),
            })

        return citations
