# Generates parity recordings for ragleap.evaluation.compare_runs by running the real Python code on generated runs.
# Writes eval_compare_cases.json into RAGLEAP_RAG_PARITY_DIR (default /root/fixtures-rag-eval). Every float is the string
# "h:" + its 16-digit hex bit pattern; None is null; counts are plain integers.
# Pinned commit: c91ef89c469a9894c2e852f86cacb4e5de462099 (packages/ragleap-rag, evaluation.py is standard library only).
# Export first: git archive c91ef89c469a9894c2e852f86cacb4e5de462099 packages/ragleap-rag/src/ragleap/evaluation.py | tar -x -C /root/py-rag-eval-pinned
import importlib.util, json, os, random, struct, sys
SRC = os.environ.get("RAGLEAP_RAG_EVAL_SRC", "/root/py-rag-eval-pinned/packages/ragleap-rag/src/ragleap/evaluation.py")
spec = importlib.util.spec_from_file_location("ragleap_evaluation_pinned", SRC)
E = importlib.util.module_from_spec(spec)
spec.loader.exec_module(E)
assert E.__file__ == SRC
OUT = os.environ.get("RAGLEAP_RAG_PARITY_DIR", "/root/fixtures-rag-eval")
os.makedirs(OUT, exist_ok=True)


def enc(v):
    if v is None or isinstance(v, (bool, str)):
        return v
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return "h:" + struct.pack(">d", v).hex()
    if isinstance(v, dict):
        return {k: enc(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [enc(x) for x in v]
    raise AssertionError(type(v))


VN = [0.0, 1.0, 0.5, 0.25, 0.1, 0.2, 0.3, 0.1 + 0.2, 0.7, 1 / 3, 0.5 + 1e-13, 0.5 + 1e-12, 0.5 + 2e-12, 0.5 + 1e-9, 0.75]
V = [None] + VN
NAMES = ["hit@1", "recall@1", "precision@1", "ndcg@1", "reciprocal_rank", "hit@3", "ndcg@10"]
rng = random.Random(20261106)


def case(q, hit, kw, gr, sup, metrics):
    return {"query": q, "retrieval_hit": hit, "keyword_coverage": kw, "groundedness": gr, "support": sup, "metrics": metrics}


def mk_run(queries, no_summary_chance=0.1):
    cases = []
    for q in queries:
        m = None
        if rng.random() < 0.7:
            m = {n: rng.choice(VN) for n in NAMES if rng.random() < 0.7}
        cases.append(case(q, rng.choice([None, True, False]), rng.choice(V), rng.choice(V), rng.choice(V), m))
    summary = None
    if rng.random() >= no_summary_chance:
        summary = {"retrieval": {n: {"mean": rng.choice(VN)} for n in NAMES if rng.random() < 0.6},
                   "support": rng.choice([None, {"mean": rng.choice(VN)}]),
                   "latency_s": rng.choice([None, {"mean": rng.uniform(0.1, 3.0), "p95": rng.uniform(0.1, 9.0)}])}
    return {"retrieval_hit_rate": rng.choice(V), "keyword_coverage_rate": rng.choice(V), "groundedness_rate": rng.choice(V),
            "results": cases, "summary": summary}


SC = []


def add(name, base, cand, tol):
    try:
        out = enc(E.compare_runs(base, cand, tol))
    except ValueError as e:
        out = {"error": str(e)}
    SC.append({"name": name, "base": enc(base), "cand": enc(cand), "tolerance": enc(float(tol)), "out": out})


Q = ["q0", "q1", "q2", "q3", "q4", "q5"]
UQ = ["\u00e9?", "\U0001F600", "\u65e5\u672c\u8a9e"]
TOLS = [0.0, 0.0, 1e-12, 1e-9, 0.05, 0.1, 0.5]
b = mk_run(Q[:3], 0.0)
add("identical", b, b, 0.0)
add("empty_runs", {"results": [], "summary": None}, {"results": [], "summary": None}, 0.0)
add("err_negative_tolerance", b, b, -0.1)
add("err_negative_tolerance_first", mk_run(Q[:2]), mk_run(Q[:3]), -2.0)
add("err_length_mismatch", mk_run(Q[:2]), mk_run(Q[:3]), 0.0)
c = mk_run(Q[:3], 0.0)
c["results"][1]["query"] = "other"
add("err_query_mismatch", b, c, 0.0)
add("unicode_queries", mk_run(UQ, 0.0), mk_run(UQ, 0.0), 0.0)
for i in range(150):
    n = rng.randint(1, 6)
    add("rand_%03d" % i, mk_run(Q[:n]), mk_run(Q[:n]), rng.choice(TOLS))
# longer baseline than candidate (the error path in the other direction)
add("err_baseline_longer", mk_run(Q[:3]), mk_run(Q[:2]), 0.0)
# exact binary ties at the 12th decimal: 2**-13 = 0.0001220703125 has 13 decimals ending in 5, so round(x, 12) must
# pick the even neighbour (half-even) where half-up would not
def tie_run(value):
    return {"retrieval_hit_rate": value, "keyword_coverage_rate": None, "groundedness_rate": None,
            "results": [case("t", None, value, None, None, {"ndcg@1": value})], "summary": None}


for k in (1, 3, 5, 7, 9, 11, 13, 15, 17, 19, 21):
    d = k * 2.0 ** -13
    add("tie_up_%d" % k, tie_run(0.0), tie_run(d), 0.0)
    add("tie_down_%d" % k, tie_run(d), tie_run(0.0), 0.0)
    add("tie_up_%d_big" % k, tie_run(0.5), tie_run(0.5 + d), 0.0)

json.dump({"python": sys.version.split()[0], "pin": os.environ.get("RAGLEAP_PIN"), "scenarios": SC},
          open(os.path.join(OUT, "eval_compare_cases.json"), "w"), allow_nan=False)
errs = sum(1 for s in SC if "error" in s["out"])
chg = sum(len(s["out"].get("case_changes", [])) for s in SC)
reg = sum(1 for s in SC if s["out"].get("has_regressions"))
print("python:", sys.version.split()[0], "| scenarios:", len(SC), "| errors:", errs, "| case changes:", chg,
      "| with regressions:", reg, "| with latency:", sum(1 for s in SC if s["out"].get("latency_s")))
print("written to", OUT)
