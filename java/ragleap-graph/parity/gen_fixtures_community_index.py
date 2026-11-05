# Generates parity recordings for the GraphIndex community methods (detect_communities, refresh_communities,
# find_communities) by running the real Python methods against a fake Neo4j session that records every statement and
# parameter, every audit call and every summarizer input. Writes community_index_cases.json into
# RAGLEAP_GRAPH_PARITY_DIR (default /root/fixtures-graph-comm).
# Pinned commit: 6892d8c83e2ffc52a4071cb456700bc761a73193 (packages/ragleap-graph).
# Export first: git archive 6892d8c83e2ffc52a4071cb456700bc761a73193 packages/ragleap-graph | tar -x -C /root/py-graph-comm-pinned2
import copy, json, logging, os, sys
logging.disable(logging.CRITICAL)
SRC = os.environ.get("RAGLEAP_GRAPH_SRC", "/root/py-graph-comm-pinned2/packages/ragleap-graph/src")
sys.path.insert(0, SRC)
import ragleap_graph as rg
assert rg.__file__.startswith(SRC), "wrong module path"
from ragleap_graph import GraphIndex, _composite_key
from ragleap_graph.communities import detect_communities as _detect
OUT = os.environ.get("RAGLEAP_GRAPH_PARITY_DIR", "/root/fixtures-graph-comm")
os.makedirs(OUT, exist_ok=True)

TEXTS = {n[len("_COMMUNITY_"):].lower(): getattr(GraphIndex, n) for n in dir(GraphIndex)
         if n.startswith("_COMMUNITY_") and isinstance(getattr(GraphIndex, n), str)}
NAME_BY_TEXT = {v: k for k, v in TEXTS.items()}
assert len(NAME_BY_TEXT) == len(TEXTS)


class Audit:
    def __init__(self, events):
        self.events = events

    def log(self, user_id, namespace, action, document_id=None, entity_count=None, detail=None):
        self.events.append(["audit", action, user_id, namespace, document_id, entity_count, copy.deepcopy(detail)])


class Session:
    def __init__(self, fake):
        self.fake = fake

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def run(self, query, **params):
        f = self.fake
        name = NAME_BY_TEXT[query]
        f.events.append(["run", name, copy.deepcopy(params)])
        n = f.counts[name] = f.counts.get(name, 0) + 1
        fail = f.sc.get("fail")
        if fail and fail[0] == name and fail[1] == n:
            raise Exception("boom")
        lists = f.sc.get("script", {}).get(name)
        if not lists:
            return []
        return copy.deepcopy(lists[min(n - 1, len(lists) - 1)])


class Fake:
    def __init__(self, sc, events):
        self.sc, self.events, self.counts = sc, events, {}

    def session(self):
        return Session(self)


def make_summ(kind, events):
    state = {"n": 0}

    def f(info):
        state["n"] += 1
        events.append(["summarizer", copy.deepcopy(info)])
        if kind in ("text", "fail_second"):
            if kind == "fail_second" and state["n"] == 2:
                raise RuntimeError("nope")
            return "  S:%d:%s  " % (info["size"], ",".join(info["display_names"])) + "  "[:0]
        if kind == "blank":
            return "   "
        if kind == "raise":
            raise RuntimeError("nope")
        if kind == "none":
            return None
        if kind == "long":
            return "x" * 20005
        if kind == "astral":
            return "\U0001F600" * 20001
        raise AssertionError(kind)
    return f


def R(edges):
    return [{"a": a, "b": b, "weight": w} for a, b, w in edges]


CO = [("a1", "a2", 2.0), ("a2", "a3", 1.5), ("a1", "a3", 1.0), ("b1", "b2", 3.0), ("b2", "b3", 1.0),
      ("b1", "b3", 1.0), ("a3", "b1", 0.5), ("x", "y", 1.0)]
REL = [("a1", "a2", 0.5), ("c1", "c2", 2.0)]
DISP = [
    [{"name": "a1", "display_name": "A1"}, {"name": "a2", "display_name": "A2"}, {"name": "a3", "display_name": ""}],
    [{"name": "b1", "display_name": "B1"}, {"name": "b2", "display_name": None}, {"name": "b3", "display_name": "B3"}],
    [{"name": "x", "display_name": "X"}, {"name": "y", "display_name": "Y"}],
]
RELS = [[{"subject": "A1", "relation_type": "WORKS_WITH", "object": "A2", "weight": 2}], [],
        [{"subject": "X", "relation_type": "KNOWS", "object": "Y", "weight": 1.5},
         {"subject": "Y", "relation_type": "KNOWS", "object": "X", "weight": 0.5}]]
B = {"co_occurs_edges": [R(CO)], "relates_edges": [R(REL)], "display_names": DISP, "relations": RELS}


def B_(**extra):
    d = dict(B)
    d.update(extra)
    return d


def keys_for(ns, uid, edges):
    return [_composite_key(ns, uid, "community", c["members_hash"]) for c in _detect(edges, resolution=1.0, min_size=2)]


def EX(keys, flags):
    return [{"key": k, "has_summary": f} for k, f in zip(keys, flags)]


K = keys_for("", "", CO)
K1 = keys_for("ns1", "u1", CO)
SC = []


def add(name, op, script=None, driver=True, fail=None, summarizer=None, **kw):
    SC.append(dict(name=name, op=op, kwargs=kw, script=script if script is not None else {}, driver=driver,
                   fail=fail, summarizer=summarizer))


# ---- detect
add("detect_basic", "detect", B)
add("detect_ns_user_relations", "detect", B, namespace="ns1", user_id="u1", include_relations=True)
add("detect_res2_min1", "detect", B, resolution=2.0, min_size=1)
add("detect_min3", "detect", B, min_size=3)
add("detect_empty", "detect", {"co_occurs_edges": [[]]})
add("detect_max_edges_exact", "detect", B, max_edges=8)
add("detect_max_edges_exceeded", "detect", B, max_edges=7)
add("detect_relations_exceeded", "detect", {"co_occurs_edges": [R(CO[:2])], "relates_edges": [R(REL + [("d1", "d2", 1.0)])]},
    include_relations=True, max_edges=2)
add("detect_fail_edges", "detect", B, fail=["co_occurs_edges", 1])
add("detect_no_driver", "detect", B, driver=False)
add("detect_bad_resolution_zero", "detect", B, resolution=0.0)
add("detect_bad_resolution_negative", "detect", B, resolution=-1.0)
add("detect_bad_min_size", "detect", B, min_size=0)
add("detect_bad_max_edges", "detect", B, max_edges=0)
add("detect_bad_args_no_driver", "detect", B, driver=False, resolution=0.0)
add("detect_weird_rows", "detect", {"co_occurs_edges": [[
    {"a": "p", "b": "q", "weight": None}, {"a": "p", "b": "q", "weight": "3"}, {"a": "p", "b": "r", "weight": 0},
    {"a": "q", "b": "r", "weight": -1.0}, {"a": "p", "b": "q", "weight": 2.0}, {"a": "q", "b": "r", "weight": 2.0}]]},
    min_size=1)
# ---- refresh
add("refresh_fresh", "refresh", B)
add("refresh_fresh_text", "refresh", B, summarizer="text")
add("refresh_relations_text_ns", "refresh", B, summarizer="text", namespace="ns1", user_id="u1", include_relations=True)
add("refresh_kept_all", "refresh", B_(existing=[EX(K, [True, True, True])]), summarizer="text")
add("refresh_kept_no_summary", "refresh", B_(existing=[EX(K, [True, False, True])]), summarizer="text")
add("refresh_force", "refresh", B_(existing=[EX(K, [True, True, True])]), summarizer="text", force=True)
add("refresh_stale_removed", "refresh", B_(existing=[EX(K[:2], [True, True]) + EX(["zz-stale", "aa-stale"], [True, False])]),
    summarizer="text")
add("refresh_kept_ns_user", "refresh", B_(existing=[EX(K1, [True, True, False])]), summarizer="text", namespace="ns1", user_id="u1")
add("refresh_summ_blank", "refresh", B, summarizer="blank")
add("refresh_summ_raise", "refresh", B, summarizer="raise")
add("refresh_summ_none", "refresh", B, summarizer="none")
add("refresh_summ_long", "refresh", B, summarizer="long")
add("refresh_summ_astral", "refresh", B, summarizer="astral")
add("refresh_summ_fail_second", "refresh", B, summarizer="fail_second")
add("refresh_limit1", "refresh", B, summarizer="text", max_summary_items=1)
add("refresh_resolution_min", "refresh", B, resolution=2.0, min_size=1, max_edges=100)
add("refresh_empty_graph", "refresh", {"co_occurs_edges": [[]], "existing": [EX(["old1"], [True])]}, summarizer="text")
add("refresh_fail_edges", "refresh", B, fail=["co_occurs_edges", 1])
add("refresh_fail_constraint", "refresh", B, fail=["constraint", 1])
add("refresh_fail_merge", "refresh", B, fail=["merge", 2])
add("refresh_fail_members", "refresh", B, fail=["members", 1])
add("refresh_fail_delete", "refresh", B_(existing=[EX(["gone"], [True])]), fail=["delete", 1])
add("refresh_fail_set_summary", "refresh", B, summarizer="text", fail=["set_summary", 1])
add("refresh_no_driver", "refresh", B, driver=False)
add("refresh_bad_resolution", "refresh", B, resolution=0.0)
add("refresh_bad_min_size", "refresh", B, min_size=0)
add("refresh_bad_max_edges", "refresh", B, max_edges=0)
add("refresh_bad_max_summary_items", "refresh", B, max_summary_items=0)
add("refresh_edges_exceeded", "refresh", B, max_edges=7)
add("refresh_bad_args_no_driver", "refresh", B, driver=False, max_summary_items=0)
# ---- find
ROWS = [{"community_id": "k1", "member_count": 4, "internal_weight": 3.5, "summary": "S1", "matched_entities": 2},
        {"community_id": "k2", "member_count": 2, "internal_weight": 1.0, "summary": None, "matched_entities": 1}]
NONE_ROWS = [{"community_id": "k3", "member_count": None, "internal_weight": None, "summary": None, "matched_entities": None}]
MEMBERS = [[{"entity_name": "A"}, {"entity_name": "B"}], [{"entity_name": "C"}]]
FS = {"list": [ROWS], "by_entity": [ROWS], "member_names": MEMBERS}
add("find_list", "find", FS)
add("find_list_members", "find", FS, include_members=True)
add("find_entities", "find", FS, entity_names=["Helio Grid", "HELIO GRID", "Orion Cells", "ab", "  "], namespace="ns1",
    user_id="u1", limit=3)
add("find_entities_members", "find", FS, entity_names=["Helio Grid"], include_members=True)
add("find_empty_list", "find", FS, entity_names=[])
add("find_blank_names", "find", FS, entity_names=["  ", "ab"])
add("find_limit_zero", "find", FS, limit=0)
add("find_limit_negative_entities", "find", FS, entity_names=["Orion Cells"], limit=-5)
add("find_none_values", "find", {"list": [NONE_ROWS], "member_names": [[]]}, include_members=True)
add("find_fail_list", "find", FS, fail=["list", 1])
add("find_fail_members", "find", FS, entity_names=["Orion Cells"], include_members=True, fail=["member_names", 2])
add("find_no_driver", "find", FS, driver=False)

DEF = {"detect": dict(namespace=None, user_id=None, resolution=1.0, min_size=2, include_relations=False, max_edges=200000),
       "refresh": dict(namespace=None, user_id=None, resolution=1.0, min_size=2, include_relations=False,
                       max_edges=200000, force=False, max_summary_items=20),
       "find": dict(entity_names=None, namespace=None, user_id=None, limit=25, include_members=False)}
METHOD = {"detect": "detect_communities", "refresh": "refresh_communities", "find": "find_communities"}


def run(sc):
    ev = []
    g = GraphIndex.__new__(GraphIndex)
    g.driver = Fake(sc, ev) if sc["driver"] else None
    g._audit = Audit(ev)
    kw = dict(DEF[sc["op"]])
    kw.update(sc["kwargs"])
    call = dict(kw)
    if sc["op"] == "refresh" and sc["summarizer"]:
        call["summarizer"] = make_summ(sc["summarizer"], ev)
    out = dict(name=sc["name"], op=sc["op"], kwargs=kw, script=sc["script"], driver=sc["driver"], fail=sc["fail"],
               summarizer=sc["summarizer"])
    try:
        out["return"] = getattr(g, METHOD[sc["op"]])(**call)
    except ValueError as e:
        out["raises"] = "ValueError"
        out["message"] = str(e)
    out["events"] = ev
    return out


results = [run(sc) for sc in SC]
json.dump({"python": sys.version.split()[0], "pin": os.environ.get("RAGLEAP_PIN"), "texts": TEXTS, "scenarios": results},
          open(os.path.join(OUT, "community_index_cases.json"), "w"), allow_nan=False)
print("python:", sys.version.split()[0], "| cypher texts:", len(TEXTS), "| scenarios:", len(results),
      "| raising:", sum(1 for r in results if "raises" in r), "| events:", sum(len(r["events"]) for r in results))
print("by op:", {op: sum(1 for r in results if r["op"] == op) for op in DEF})
print("summarizer calls recorded:", sum(1 for r in results for e in r["events"] if e[0] == "summarizer"),
      "| refresh results success:", sum(1 for r in results if r["op"] == "refresh" and r.get("return", {}).get("success")))
print("written to", OUT)
