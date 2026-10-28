"""Records the real Python GraphIndex (pinned export) against a fake Neo4j driver."""
import glob, json, logging, os, sys

logging.disable(logging.CRITICAL)
hits = glob.glob("/tmp/py-graph-pinned/**/ragleap_graph/__init__.py", recursive=True)
assert len(hits) == 1, hits
sys.path.insert(0, os.path.dirname(os.path.dirname(hits[0])))
import ragleap_graph as mod

assert os.path.realpath(mod.__file__) == os.path.realpath(hits[0]), mod.__file__


class FakeSession:
    def __init__(self, drv):
        self.drv = drv

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, stmt, *a, **k):
        d = self.drv
        d.attempts.append(stmt)
        if stmt.startswith("CREATE CONSTRAINT"):
            d.constraint_calls += 1
            if d.constraint_fail_at == d.constraint_calls:
                raise RuntimeError("boom: constraint")
        if stmt == "RETURN 1" and d.health_fails:
            raise RuntimeError("boom: health")
        return None


class FakeDriver:
    def __init__(self, connect_fails, constraint_fail_at, health_fails):
        self.connect_fails = connect_fails
        self.constraint_fail_at = constraint_fail_at
        self.health_fails = health_fails
        self.attempts = []
        self.constraint_calls = 0
        self.close_calls = 0

    def verify_connectivity(self):
        if self.connect_fails:
            raise RuntimeError("boom: connect")

    def session(self):
        return FakeSession(self)

    def close(self):
        self.close_calls += 1


class FakeGD:
    current = None

    @staticmethod
    def driver(uri, auth=None):
        return FakeGD.current


mod.GraphDatabase = FakeGD


def build(connect_fails=False, constraint_fail_at=None, health_fails=False):
    drv = FakeDriver(connect_fails, constraint_fail_at, health_fails)
    FakeGD.current = drv
    return mod.GraphIndex(), drv


SCENARIOS = [
    ("ok", False, None, False),
    ("connect_fails", True, None, False),
    ("constraint_fail_at_1", False, 1, False),
    ("constraint_fail_at_3", False, 3, False),
    ("constraint_fail_at_6", False, 6, False),
    ("health_fails", False, None, True),
    ("constraint_fail_at_3_and_health_fails", False, 3, True),
]
scenarios = []
for name, cf, ca, hf in SCENARIOS:
    idx, drv = build(cf, ca, hf)
    available = idx.driver is not None
    attempted = [s for s in drv.attempts if s.startswith("CREATE CONSTRAINT")]
    after_construct = drv.close_calls
    health = idx.health_check()
    idx.close()
    scenarios.append({
        "name": name, "connect_fails": cf, "constraint_fail_at": ca, "health_fails": hf,
        "expected": {
            "available": available,
            "constraints_attempted": attempted,
            "driver_close_calls_after_construct": after_construct,
            "health_check": health,
            "driver_close_calls_after_close": drv.close_calls,
        },
    })
ok = scenarios[0]["expected"]
assert ok["available"] and len(ok["constraints_attempted"]) == 6 and ok["health_check"], ok

idx, _drv = build()
QUERIES = [
    ("AAA BBB CCC DDD EEE FFF GGG HHH III JJJ KKK LLL", None, None),
    ("AAA BBB CCC DDD EEE FFF GGG HHH III JJJ KKK LLL", 12, None),
    ("What did Acme Corp launch?", None, None),
    ("tell me about the alara program", None, ["alara"]),
    ("IAEA and ALARA", 1, None),
    ("", None, None),
]
query = []
for text, max_entities, domain_terms in QUERIES:
    kwargs = {}
    if max_entities is not None:
        kwargs["max_entities"] = max_entities
    if domain_terms is not None:
        kwargs["domain_terms"] = domain_terms
    query.append({"query": text, "max_entities": max_entities, "domain_terms": domain_terms,
                  "expected": idx.extract_query_entities(text, **kwargs)})

out = "/tmp/fixtures-graph-a/index_cases.json"
json.dump({"scenarios": scenarios, "query": query}, open(out, "w"), indent=1)
print("wrote", out, "scenarios", len(scenarios), "query", len(query))
