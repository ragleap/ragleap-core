"""Records the real Python GraphIndex.upsert_document (pinned export): statements, retry sleeps and log messages."""
import glob, json, logging, os, sys
import random as real_random
import time as real_time

hits = glob.glob("/tmp/py-graph-pinned/**/ragleap_graph/__init__.py", recursive=True)
assert len(hits) == 1, hits
sys.path.insert(0, os.path.dirname(os.path.dirname(hits[0])))
import ragleap_graph as mod

assert os.path.realpath(mod.__file__) == os.path.realpath(hits[0]), mod.__file__


class TransientFake(Exception):
    pass


mod.TransientError = TransientFake
SLEEPS = []
LOGS = []


class FakeTime:
    def __getattr__(self, name):
        return getattr(real_time, name)

    def sleep(self, seconds):
        SLEEPS.append(seconds)


class FakeRandom:
    def __getattr__(self, name):
        return getattr(real_random, name)

    def uniform(self, a, b):
        return a + (b - a) * 0.5


mod.time = FakeTime()
mod.random = FakeRandom()


class Capture(logging.Handler):
    def emit(self, record):
        if record.levelno >= logging.WARNING:
            LOGS.append([record.levelname, record.getMessage()])


assert hasattr(mod, "logger"), "module has no logger"


class FakeSession:
    def __init__(self, drv):
        self.drv = drv
        drv.attempt += 1
        self.n = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def run(self, query, **params):
        d = self.drv
        self.n += 1
        d.calls.append({"cypher": "".join(query.split()), "params": params})
        f = d.fail
        if f and d.attempt <= f["attempts_failing"] and self.n == f["at_call"]:
            if f["kind"] == "transient":
                raise TransientFake("boom: transient")
            raise RuntimeError("boom: hard")
        if "RETURN pw.entity_a AS a" in query:
            return [{"a": a, "b": b} for a, b in d.old_pairs]
        if "RETURN rw.subject AS subject" in query:
            return [{"subject": s, "relation_type": t, "object": o} for s, t, o in d.old_relations]
        return []


class FakeDriver:
    def __init__(self, old_pairs, old_relations, fail):
        self.calls = []
        self.attempt = 0
        self.old_pairs = [tuple(x) for x in old_pairs]
        self.old_relations = [tuple(x) for x in old_relations]
        self.fail = fail

    def session(self):
        return FakeSession(self)

    def close(self):
        pass


def S(name, chunks, fail=None, no_driver=False, **kw):
    d = dict(name=name, document_id="doc-" + name, title="T", chunks=chunks, namespace=None, user_id=None,
             max_entities=80, max_pairs=150, domain_terms=None, old_pairs=[], old_relations=[],
             fail=fail, no_driver=no_driver)
    d.update(kw)
    return d


BASIC = [{"text": "Acme Corp announced a partnership with Globex Industries."}]
SCEN = [
    S("pair_order_case", [{"text": "ABD works with Abc Corp."}]),
    S("weights_two", [{"text": "Acme Corp and Globex Industries."}, {"text": "Acme Corp and Globex Industries."}]),
    S("transient_once", BASIC, fail={"attempts_failing": 1, "at_call": 4, "kind": "transient"}),
    S("transient_twice", BASIC, fail={"attempts_failing": 2, "at_call": 8, "kind": "transient"}),
    S("transient_always", BASIC, fail={"attempts_failing": 3, "at_call": 4, "kind": "transient"}),
    S("hard_fail_at_4", BASIC, fail={"attempts_failing": 1, "at_call": 4, "kind": "hard"}),
    S("hard_fail_at_8", BASIC, fail={"attempts_failing": 1, "at_call": 8, "kind": "hard"}),
    S("no_driver", BASIC, no_driver=True),
]
g = mod.GraphIndex(config=mod.GraphConfig(uri="bolt://localhost:1", user="x", password="x"))
mod.logger.addHandler(Capture())
mod.logger.setLevel(logging.DEBUG)
mod.logger.propagate = False
out = []
for sc in SCEN:
    SLEEPS.clear()
    LOGS.clear()
    drv = FakeDriver(sc["old_pairs"], sc["old_relations"], sc["fail"])
    g.driver = None if sc["no_driver"] else drv
    summary = g.upsert_document(
        document_id=sc["document_id"], title=sc["title"], chunks=sc["chunks"], namespace=sc["namespace"],
        user_id=sc["user_id"], max_entities=sc["max_entities"], max_pairs=sc["max_pairs"],
        domain_terms=sc["domain_terms"])
    out.append({"name": sc["name"],
                "args": {k: sc[k] for k in ("document_id", "title", "chunks", "namespace", "user_id",
                                            "max_entities", "max_pairs", "domain_terms")},
                "old_pairs": sc["old_pairs"], "old_relations": sc["old_relations"],
                "fail": sc["fail"], "no_driver": sc["no_driver"],
                "summary": summary, "calls": drv.calls, "sleeps": list(SLEEPS), "logs": [list(x) for x in LOGS]})
byname = {o["name"]: o for o in out}
po = byname["pair_order_case"]
assert po["summary"]["success"] and po["summary"]["entities_indexed"] == 2 and po["summary"]["relationships_indexed"] == 1, po["summary"]
assert any(c["params"].get("a") == "abc corp" and c["params"].get("b") == "abd" for c in po["calls"]), "pair order case does not order by lowercase"
assert byname["transient_once"]["summary"]["success"] and len(byname["transient_once"]["sleeps"]) == 1
assert len(byname["transient_twice"]["sleeps"]) == 2 and byname["transient_twice"]["summary"]["success"]
assert not byname["transient_always"]["summary"]["success"] and len(byname["transient_always"]["sleeps"]) == 2
assert byname["no_driver"]["summary"]["error"] == "Neo4j driver not available" and not byname["no_driver"]["calls"]
OUT = "/tmp/fixtures-graph-a/upsert_retry_cases.json"
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=True, indent=1)
print("wrote", OUT, "cases", len(out))
for o in out:
    print(" ", o["name"], "| calls", len(o["calls"]), "| sleeps", o["sleeps"], "| logs", len(o["logs"]), "|", o["summary"]["success"], o["summary"]["error"])
