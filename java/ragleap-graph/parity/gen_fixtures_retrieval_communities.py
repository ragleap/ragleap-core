# Generates parity recordings for GraphRetriever with include_communities (and the new config fields) by running the
# real Python classes against fake graph and rag objects that append every call to ONE ordered event log. Writes
# retrieval_communities_cases.json into RAGLEAP_GRAPH_PARITY_DIR (default /root/fixtures-graph-comm).
# Pinned commit: 94575d990d89b0e328bffdc14f4fa845d95b0153 (packages/ragleap-graph).
# Export first: git archive 94575d990d89b0e328bffdc14f4fa845d95b0153 packages/ragleap-graph | tar -x -C /root/py-graph-comm-pinned3
import json, logging, os, sys
logging.disable(logging.CRITICAL)
SRC = os.environ.get("RAGLEAP_GRAPH_SRC", "/root/py-graph-comm-pinned3/packages/ragleap-graph/src")
sys.path.insert(0, SRC)
import ragleap_graph as rg
assert rg.__file__.startswith(SRC), "wrong module path"
from ragleap_graph.retrieval import GraphRetriever, GraphRetrievalConfig
OUT = os.environ.get("RAGLEAP_GRAPH_PARITY_DIR", "/root/fixtures-graph-comm")
os.makedirs(OUT, exist_ok=True)


class FakeGraph:
    def __init__(self, sc, log):
        self.sc, self.log = sc, log

    def extract_query_entities(self, query, max_entities=10, domain_terms=None):
        self.log.append(["extract", query, max_entities, domain_terms])
        return self.sc["query_entities"]

    def find_documents_by_entities(self, entity_names, namespace=None, limit=25):
        self.log.append(["find_docs", list(entity_names), namespace, limit])
        return [dict(d) for d in self.sc["docs"]]

    def search_related_entities(self, entity_names, namespace=None, max_depth=2, limit=10):
        self.log.append(["search_related", list(entity_names), namespace, max_depth, limit])
        return [dict(e) for e in self.sc["related"]]

    def find_communities(self, entity_names=None, namespace=None, user_id=None, limit=25, include_members=False):
        assert include_members is False
        self.log.append(["find_comm", list(entity_names), namespace, user_id, limit])
        return [dict(c) for c in self.sc["communities"]]


class FakeRag:
    def __init__(self, sc, log):
        self.sc, self.log = sc, log

    def retrieve(self, query, **kwargs):
        self.log.append(["rag", query, kwargs.get("top_k"), kwargs.get("metadata_filter"), sorted(kwargs)])
        return [dict(c) for c in self.sc["chunks"]]


def D(i, name, n=1, score=1.0, names=None):
    return {"document_id": i, "document_name": name, "matched_entities": n, "graph_score": score,
            "matched_entity_names": names if names is not None else ["Acme Corp"]}


def E(i, name, rel="CO_OCCURS_WITH", depth=1):
    return {"entity_id": i, "entity_name": name, "relationship": rel, "depth": depth}


def CK(cid, doc, name, text, idx=0):
    return {"chunk_id": cid, "text": text, "document_id": doc, "document_name": name, "chunk_index": idx}


def COM(cid, size, summary, matched=1, weight=3.5):
    return {"community_id": cid, "size": size, "internal_weight": weight, "summary": summary, "matched_entities": matched}


def SC(name, **kw):
    d = dict(name=name, query="What products has Acme Corp launched?", call={}, config={"include_communities": True},
             query_entities=["Acme Corp"], docs=[D("d3", "acme_contract.pdf", 1, 2.5)], related=[E("neo4j", "Neo4j")],
             communities=[COM("k1", 4, "Acme and its partners."), COM("k2", 2, None, 1, 1.0)],
             chunks=[CK("c1", "d1", "q3_report.pdf", "Acme Corp reported strong Q3 revenue growth."),
                     CK("c2", "d2", "press_release.pdf", "The new product line launched in partnership with Neo4j.")],
             rag_none=False)
    d.update(kw)
    return d


S = [
    SC("comm_basic"),
    SC("comm_off_explicit", config={"include_communities": False}),
    SC("comm_off_default", config={}),
    SC("comm_max_two", config={"include_communities": True, "max_communities": 2}),
    SC("comm_max_ignored_when_off", config={"max_communities": 9}),
    SC("comm_namespace_and_args", query="\U0001F600 \u00c9cole?", call={"top_k": 1, "namespace": "ns-\u00e9"}),
    SC("comm_no_entities", query_entities=[]),
    SC("comm_no_entities_graph_only", query_entities=[], config={"include_communities": True, "mode": "graph_only"}),
    SC("comm_graph_only", config={"include_communities": True, "mode": "graph_only"}),
    SC("comm_empty_list", communities=[]),
    SC("comm_rag_none_hybrid", rag_none=True),
    SC("comm_rag_none_graph_only", rag_none=True, config={"include_communities": True, "mode": "graph_only"}),
    SC("comm_summary_lengths", communities=[COM("a", 2, "x" * 149), COM("b", 2, "x" * 150), COM("c", 2, "x" * 151),
                                            COM("d", 2, ""), COM("e", 2, "x" * 300)]),
    SC("comm_summary_emoji", communities=[COM("a", 2, "a" * 149 + "\U0001F600" + "b"),
                                          COM("b", 2, "a" * 148 + "\U0001F600\U0001F600"),
                                          COM("c", 2, "\U0001F600" * 151), COM("d", 2, "\u00c9cole \u4e2d\u6587 " * 40)]),
    SC("comm_values", communities=[COM("k", 0, "s", 0, 0.0), COM("l", 12, "t", 7, 1e-9)]),
    SC("comm_order", chunks=[CK("c1", "d1", "a.pdf", "one"), CK("c2", "d2", "b.pdf", "two")],
       related=[E("n1", "N1"), E("n2", "N2", "CONTAINS", 2)],
       communities=[COM("k1", 3, "first"), COM("k2", 2, "second"), COM("k3", 2, None)]),
    SC("comm_duplicate_entities", query_entities=["Acme", "acme", "Acme"]),
    SC("comm_overlap_flags", chunks=[CK("c1", "d1", "a.pdf", "Acme Corp news.")],
       docs=[D("d1", "a.pdf"), D("d9", "b.pdf", 1, 1.0, ["Globex Corp"])]),
]


def run(sc):
    log = []
    cfg = GraphRetrievalConfig(**sc["config"])
    retriever = GraphRetriever(graph=FakeGraph(sc, log), rag=None if sc["rag_none"] else FakeRag(sc, log), config=cfg)
    rec = {k: sc[k] for k in ("name", "query", "call", "config", "query_entities", "docs", "related", "communities",
                              "chunks", "rag_none")}
    try:
        rec["result"] = retriever.retrieve(sc["query"], **sc["call"])
    except Exception as e:
        rec["raises"] = type(e).__name__
        rec["message"] = str(e)
    rec["events"] = log
    return rec


def CC(name, **kw):
    rec = {"name": name, "kwargs": kw}
    try:
        c = GraphRetrievalConfig(**kw)
        rec["ok"] = True
        rec["fields"] = {"mode": c.mode, "graph_expand_depth": c.graph_expand_depth,
                         "max_graph_results": c.max_graph_results, "max_query_entities": c.max_query_entities,
                         "include_communities": c.include_communities, "max_communities": c.max_communities}
    except Exception as e:
        rec["ok"] = False
        rec["error_type"] = type(e).__name__
        rec["message"] = str(e)
    return rec


CFG = [
    CC("defaults"), CC("include_on", include_communities=True), CC("include_off", include_communities=False),
    CC("max_five", include_communities=True, max_communities=5), CC("max_one", max_communities=1),
    CC("max_big", max_communities=1000),
    CC("max_zero", max_communities=0), CC("max_negative", max_communities=-3),
    CC("max_zero_with_include", include_communities=True, max_communities=0),
    CC("order_depth_before_max", graph_expand_depth=0, max_communities=0),
    CC("order_entities_before_max", max_query_entities=0, max_communities=0),
    CC("order_mode_before_max", mode="x", max_communities=0),
]
retrieve = [run(sc) for sc in S]
json.dump({"python": sys.version.split()[0], "pin": os.environ.get("RAGLEAP_PIN"), "config": CFG, "retrieve": retrieve},
          open(os.path.join(OUT, "retrieval_communities_cases.json"), "w"), default=str)
print("python:", sys.version.split()[0], "| retrieve scenarios:", len(retrieve), "| config cases:", len(CFG))
print("python errors:", sorted({(r["name"], r["raises"]) for r in retrieve if "raises" in r}))
print("config errors:", sorted({c["error_type"] for c in CFG if not c["ok"]}))
print("written to", OUT)
