"""
Tests for ragleap_graph.retrieval (v0.3.0).

Uses lightweight fakes matching the real contracts of RagLeap.retrieve()
and GraphIndex's methods (extract_query_entities,
find_documents_by_entities, search_related_entities) - no live services
needed for these, since GraphRetriever itself has no I/O of its own,
it only orchestrates calls to injected graph/rag objects.
"""
import pytest

from ragleap_graph.retrieval import GraphRetriever, GraphRetrievalConfig


class FakeRag:
    """Matches RagLeap.retrieve()'s real signature and return shape."""

    def __init__(self, chunks=None):
        self._chunks = chunks if chunks is not None else [
            {"chunk_id": "c1", "text": "Acme Corp reported strong Q3 revenue growth.",
             "document_id": "d1", "document_name": "q3_report.pdf", "chunk_index": 0},
            {"chunk_id": "c2", "text": "The new product line launched in partnership with Neo4j.",
             "document_id": "d2", "document_name": "press_release.pdf", "chunk_index": 0},
        ]
        self.called_with = None

    def retrieve(self, query, top_k=5, hybrid=True, rerank=False, metadata_filter=None):
        self.called_with = {"query": query, "top_k": top_k, "metadata_filter": metadata_filter}
        return self._chunks[:top_k]


class FakeGraph:
    """Matches GraphIndex's real method signatures and return shapes."""

    def __init__(self, query_entities=None, related_documents=None, related_entities=None):
        self._query_entities = query_entities if query_entities is not None else ["Acme Corp"]
        self._related_documents = related_documents if related_documents is not None else [
            {"document_id": "d3", "document_name": "acme_contract.pdf",
             "matched_entities": 1, "graph_score": 2.5, "matched_entity_names": ["Acme Corp"]},
        ]
        self._related_entities = related_entities if related_entities is not None else [
            {"entity_id": "neo4j", "entity_name": "Neo4j", "relationship": "CO_OCCURS_WITH", "depth": 1},
        ]
        self.extract_calls = []
        self.find_docs_calls = []
        self.search_related_calls = []

    def extract_query_entities(self, query, max_entities=10, domain_terms=None):
        self.extract_calls.append({"query": query, "max_entities": max_entities, "domain_terms": domain_terms})
        return self._query_entities

    def find_documents_by_entities(self, entity_names, namespace=None, limit=25):
        self.find_docs_calls.append({"entity_names": entity_names, "namespace": namespace, "limit": limit})
        return self._related_documents

    def search_related_entities(self, entity_names, namespace=None, max_depth=2, limit=10):
        self.search_related_calls.append({"entity_names": entity_names, "namespace": namespace, "max_depth": max_depth, "limit": limit})
        return self._related_entities


# ---------------------------------------------------------------------------
# GraphRetrievalConfig - pure logic
# ---------------------------------------------------------------------------

def test_config_defaults_to_hybrid():
    config = GraphRetrievalConfig()
    assert config.mode == "hybrid"
    assert config.graph_expand_depth == 2
    assert config.max_graph_results == 10


def test_config_rejects_bad_mode():
    with pytest.raises(ValueError, match="mode must be"):
        GraphRetrievalConfig(mode="bogus")


def test_config_rejects_bad_depth():
    with pytest.raises(ValueError, match="graph_expand_depth"):
        GraphRetrievalConfig(graph_expand_depth=0)


def test_config_rejects_bad_max_graph_results():
    with pytest.raises(ValueError, match="max_graph_results"):
        GraphRetrievalConfig(max_graph_results=0)


def test_config_rejects_bad_max_query_entities():
    with pytest.raises(ValueError, match="max_query_entities"):
        GraphRetrievalConfig(max_query_entities=0)


# ---------------------------------------------------------------------------
# GraphRetriever - hybrid mode
# ---------------------------------------------------------------------------

def test_hybrid_mode_combines_chunks_and_graph_context():
    rag = FakeRag()
    graph = FakeGraph()
    retriever = GraphRetriever(graph=graph, rag=rag, config=GraphRetrievalConfig(mode="hybrid"))

    result = retriever.retrieve("What products has Acme Corp launched?", top_k=2)

    assert len(result["chunks"]) == 2
    assert result["query_entities"] == ["Acme Corp"]
    assert len(result["graph_context"]["related_documents"]) == 1
    assert len(result["graph_context"]["related_entities"]) == 1
    assert result["retrieval_method"] == "hybrid_vector_graph"


def test_hybrid_mode_calls_rag_retrieve_with_correct_args():
    rag = FakeRag()
    graph = FakeGraph()
    retriever = GraphRetriever(graph=graph, rag=rag)

    retriever.retrieve("some query", top_k=3, metadata_filter={"tenant": "acme"})

    assert rag.called_with["query"] == "some query"
    assert rag.called_with["top_k"] == 3
    assert rag.called_with["metadata_filter"] == {"tenant": "acme"}


def test_hybrid_mode_citations_include_both_chunk_and_graph_path_types():
    rag = FakeRag()
    graph = FakeGraph()
    retriever = GraphRetriever(graph=graph, rag=rag)

    result = retriever.retrieve("query")

    citation_types = [c["type"] for c in result["citations"]]
    assert citation_types.count("chunk") == 2
    assert citation_types.count("graph_path") == 1


def test_no_query_entities_skips_graph_calls_but_still_returns_chunks():
    """When the query has no extractable entities, graph lookups are
    pointless and should be skipped entirely - not called with an
    empty list."""
    rag = FakeRag()
    graph = FakeGraph(query_entities=[])
    retriever = GraphRetriever(graph=graph, rag=rag, config=GraphRetrievalConfig(mode="hybrid"))

    result = retriever.retrieve("vague query with no named entities")

    assert result["query_entities"] == []
    assert result["graph_context"] == {"related_documents": [], "related_entities": []}
    assert len(graph.find_docs_calls) == 0
    assert len(graph.search_related_calls) == 0
    assert len(result["chunks"]) == 2  # hybrid mode still returns vector chunks


# ---------------------------------------------------------------------------
# GraphRetriever - graph_only mode
# ---------------------------------------------------------------------------

def test_graph_only_mode_never_calls_rag_retrieve():
    rag = FakeRag()
    graph = FakeGraph()
    retriever = GraphRetriever(graph=graph, rag=rag, config=GraphRetrievalConfig(mode="graph_only"))

    result = retriever.retrieve("Show all documents about Acme Corp")

    assert result["chunks"] == []
    assert rag.called_with is None
    assert result["retrieval_method"] == "graph_only"


def test_graph_only_mode_still_populates_graph_context():
    rag = FakeRag()
    graph = FakeGraph()
    retriever = GraphRetriever(graph=graph, rag=rag, config=GraphRetrievalConfig(mode="graph_only"))

    result = retriever.retrieve("Acme Corp related tickets")

    assert len(result["graph_context"]["related_documents"]) == 1
    assert len(result["graph_context"]["related_entities"]) == 1


# ---------------------------------------------------------------------------
# GraphRetriever - config passthrough
# ---------------------------------------------------------------------------

def test_graph_expand_depth_and_max_results_passed_through():
    rag = FakeRag()
    graph = FakeGraph()
    config = GraphRetrievalConfig(graph_expand_depth=5, max_graph_results=3)
    retriever = GraphRetriever(graph=graph, rag=rag, config=config)

    retriever.retrieve("query about Acme Corp")

    assert graph.search_related_calls[0]["max_depth"] == 5
    assert graph.search_related_calls[0]["limit"] == 3
    assert graph.find_docs_calls[0]["limit"] == 3


def test_namespace_passed_through_to_graph_methods():
    rag = FakeRag()
    graph = FakeGraph()
    retriever = GraphRetriever(graph=graph, rag=rag)

    retriever.retrieve("query about Acme Corp", namespace="acme-tenant")

    assert graph.find_docs_calls[0]["namespace"] == "acme-tenant"
    assert graph.search_related_calls[0]["namespace"] == "acme-tenant"


def test_related_documents_flagged_when_overlapping_vector_chunks():
    """
    Regression test for a known limitation: GraphRetriever returned
    chunks (vector results) and related_documents (graph results) as
    fully separate lists, with no way for the caller to know a document
    surfaced through BOTH paths - real, meaningful overlap information
    that was silently discarded rather than exposed. Fixed by adding
    an already_in_vector_results flag to each related_documents entry,
    rather than removing overlapping entries outright - the caller
    still gets full graph-relationship context (matched entities, graph
    score) even for documents also present in the chunk list, just now
    knows about the overlap.
    """
    rag = FakeRag(chunks=[
        {"chunk_id": "c1", "text": "Acme Corp news.", "document_id": "d1",
         "document_name": "a.pdf", "chunk_index": 0},
    ])
    graph = FakeGraph(related_documents=[
        {"document_id": "d1", "document_name": "a.pdf", "matched_entities": 1,
         "graph_score": 1.0, "matched_entity_names": ["Acme Corp"]},
        {"document_id": "d9", "document_name": "b.pdf", "matched_entities": 1,
         "graph_score": 1.0, "matched_entity_names": ["Globex Corp"]},
    ])
    retriever = GraphRetriever(graph=graph, rag=rag, config=GraphRetrievalConfig())
    result = retriever.retrieve("test query")
    related = result["graph_context"]["related_documents"]
    by_id = {d["document_id"]: d for d in related}
    assert by_id["d1"]["already_in_vector_results"] is True
    assert by_id["d9"]["already_in_vector_results"] is False


def test_domain_terms_passed_through_to_extract_query_entities():
    rag = FakeRag()
    graph = FakeGraph()
    config = GraphRetrievalConfig(domain_terms=["ALARA", "dose limits"])
    retriever = GraphRetriever(graph=graph, rag=rag, config=config)

    retriever.retrieve("query")

    assert graph.extract_calls[0]["domain_terms"] == ["ALARA", "dose limits"]


# ---------------------------------------------------------------------------
# Opt-in community lookup
# ---------------------------------------------------------------------------

DEFAULT_COMMUNITIES = [
    {"community_id": "k1", "size": 4, "internal_weight": 3.0,
     "summary": "Acme and its partners.", "matched_entities": 1},
    {"community_id": "k2", "size": 2, "internal_weight": 1.0,
     "summary": None, "matched_entities": 1},
]


class FakeGraphWithCommunities(FakeGraph):
    """FakeGraph plus GraphIndex.find_communities()'s real signature."""

    def __init__(self, *args, communities=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._communities = communities if communities is not None else [
            dict(c) for c in DEFAULT_COMMUNITIES
        ]
        self.find_communities_calls = []

    def find_communities(self, entity_names=None, namespace=None, user_id=None,
                         limit=25, include_members=False):
        self.find_communities_calls.append({
            "entity_names": entity_names, "namespace": namespace, "user_id": user_id,
            "limit": limit, "include_members": include_members,
        })
        return self._communities


def test_config_communities_defaults_off():
    config = GraphRetrievalConfig()
    assert config.include_communities is False
    assert config.max_communities == 3


def test_config_rejects_bad_max_communities():
    with pytest.raises(ValueError, match="max_communities"):
        GraphRetrievalConfig(max_communities=0)


def test_config_rejects_non_bool_include_communities():
    with pytest.raises(ValueError, match="include_communities"):
        GraphRetrievalConfig(include_communities="yes")


def test_communities_off_by_default_changes_nothing():
    graph = FakeGraphWithCommunities()
    result = GraphRetriever(graph, FakeRag()).retrieve("Acme Corp news")
    assert graph.find_communities_calls == []
    assert "communities" not in result["graph_context"]
    assert all(c["type"] != "community" for c in result["citations"])


def test_communities_enabled_calls_find_communities_with_exact_args():
    graph = FakeGraphWithCommunities()
    config = GraphRetrievalConfig(include_communities=True, max_communities=2)
    GraphRetriever(graph, FakeRag(), config).retrieve("Acme Corp news", namespace="ns1")
    assert graph.find_communities_calls == [{
        "entity_names": ["Acme Corp"], "namespace": "ns1", "user_id": None,
        "limit": 2, "include_members": False,
    }]


def test_communities_enabled_adds_context_and_citations_in_order():
    graph = FakeGraphWithCommunities()
    config = GraphRetrievalConfig(include_communities=True)
    result = GraphRetriever(graph, FakeRag(), config).retrieve("Acme Corp news")
    assert result["graph_context"]["communities"] == DEFAULT_COMMUNITIES
    types = [c["type"] for c in result["citations"]]
    assert types == ["chunk", "chunk", "graph_path", "community", "community"]
    assert result["citations"][3] == {
        "type": "community", "community_id": "k1", "size": 4,
        "summary_preview": "Acme and its partners.", "matched_entities": 1,
    }
    assert result["citations"][4] == {
        "type": "community", "community_id": "k2", "size": 2,
        "summary_preview": None, "matched_entities": 1,
    }


def test_community_summary_preview_is_cut_at_150_chars():
    communities = [
        {"community_id": "a", "size": 2, "summary": "x" * 150, "matched_entities": 1},
        {"community_id": "b", "size": 2, "summary": "y" * 151, "matched_entities": 1},
    ]
    graph = FakeGraphWithCommunities(communities=communities)
    config = GraphRetrievalConfig(include_communities=True)
    result = GraphRetriever(graph, FakeRag(), config).retrieve("Acme Corp news")
    previews = {c["community_id"]: c["summary_preview"]
                for c in result["citations"] if c["type"] == "community"}
    assert previews["a"] == "x" * 150
    assert previews["b"] == "y" * 150 + "..."


def test_communities_enabled_without_query_entities_skips_lookup():
    graph = FakeGraphWithCommunities(query_entities=[])
    config = GraphRetrievalConfig(include_communities=True)
    result = GraphRetriever(graph, FakeRag(), config).retrieve("nothing here")
    assert graph.find_communities_calls == []
    assert graph.find_docs_calls == []
    assert result["graph_context"]["communities"] == []
    assert all(c["type"] != "community" for c in result["citations"])


def test_communities_work_in_graph_only_mode():
    graph = FakeGraphWithCommunities()
    rag = FakeRag()
    config = GraphRetrievalConfig(mode="graph_only", include_communities=True)
    result = GraphRetriever(graph, rag, config).retrieve("Acme Corp news")
    assert rag.called_with is None
    assert result["chunks"] == []
    assert len(result["graph_context"]["communities"]) == 2
    assert [c["type"] for c in result["citations"]] == ["graph_path", "community", "community"]
