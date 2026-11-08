# Records the real Python temporal behaviour: the statements find_relations sends (3 directions x temporal off/on x relation
# type x as_of none/UTC/+05:30) and the three temporal statement texts. Writes temporal_cases.json into
# RAGLEAP_GRAPH_PARITY_DIR (default /root/fixtures-graph-comm). as_of parameters are recorded as isoformat strings.
# Pinned commit: 85436feb203de5a4d8db37ccaa75e39268a55982 (packages/ragleap-graph).
# Export first: git archive 85436feb203de5a4d8db37ccaa75e39268a55982 packages/ragleap-graph | tar -x -C /root/py-graph-temporal-pinned
import copy, json, logging, os, sys
from datetime import datetime, timedelta, timezone
logging.disable(logging.CRITICAL)
SRC = os.environ.get("RAGLEAP_GRAPH_SRC", "/root/py-graph-temporal-pinned/packages/ragleap-graph/src")
sys.path.insert(0, SRC)
import ragleap_graph as rg
assert rg.__file__.startswith(SRC), "wrong module path"
from ragleap_graph import GraphIndex
OUT = os.environ.get("RAGLEAP_GRAPH_PARITY_DIR", "/root/fixtures-graph-comm")
os.makedirs(OUT, exist_ok=True)


class Audit:
    def log(self, *a, **k):
        pass


class Session:
    def __init__(self, calls):
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def run(self, query, **params):
        p = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in params.items()}
        self.calls.append([query, copy.deepcopy(p)])
        return []


class Driver:
    def __init__(self, calls):
        self.calls = calls

    def session(self):
        return Session(self.calls)


AS_OF = [None, datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
         datetime(2026, 3, 4, 10, 15, 30, 250000, tzinfo=timezone(timedelta(hours=5, minutes=30)))]
cases = []
for temporal in (False, True):
    for direction in ("outgoing", "incoming", "both"):
        for rtype in (None, "PARTNERED_WITH"):
            for as_of in AS_OF:
                g = GraphIndex.__new__(GraphIndex)
                calls = []
                g.driver = Driver(calls)
                g._audit = Audit()
                g.temporal = temporal
                g.find_relations("Acme Corp", relation_type=rtype, namespace="ns", direction=direction, as_of=as_of)
                cases.append({"temporal": temporal, "direction": direction, "relation_type": rtype,
                              "as_of": as_of.isoformat() if as_of else None, "calls": calls})
texts = {"refresh_temporal": GraphIndex._RELATES_AS_REFRESH_TEMPORAL,
         "comm_edges_open": GraphIndex._COMMUNITY_RELATES_EDGES_OPEN,
         "comm_relations_open": GraphIndex._COMMUNITY_RELATIONS_OPEN}
json.dump({"python": sys.version.split()[0], "pin": os.environ.get("RAGLEAP_PIN"), "texts": texts, "cases": cases},
          open(os.path.join(OUT, "temporal_cases.json"), "w"))
print("python:", sys.version.split()[0], "| cases:", len(cases), "| texts:", len(texts), "| calls:", sum(len(c["calls"]) for c in cases))
print("written to", OUT)
