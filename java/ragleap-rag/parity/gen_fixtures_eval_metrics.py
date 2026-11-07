# Generates parity recordings for the pure functions of ragleap.evaluation (retrieval_metrics, percentile, summarize)
# by running the real Python code. Writes eval_metrics_cases.txt into RAGLEAP_RAG_PARITY_DIR
# (default /root/fixtures-rag-eval). One case per line, tab separated; every double is its 16-digit hex bit pattern.
# Pinned commit: 377f194d9e25858baa968b7cc923c2ab6ef720e3 (packages/ragleap-rag, evaluation.py is standard library only).
# Export first: git archive 377f194d9e25858baa968b7cc923c2ab6ef720e3 packages/ragleap-rag/src/ragleap/evaluation.py | tar -x -C /root/py-rag-eval-pinned
# Recorded with Python 3.11. From Python 3.12 sum() of floats is compensated and can differ in the last bit.
import importlib.util, os, random, struct, sys
SRC = os.environ.get("RAGLEAP_RAG_EVAL_SRC", "/root/py-rag-eval-pinned/packages/ragleap-rag/src/ragleap/evaluation.py")
spec = importlib.util.spec_from_file_location("ragleap_evaluation_pinned", SRC)
E = importlib.util.module_from_spec(spec)
spec.loader.exec_module(E)
assert E.__file__ == SRC
OUT = os.environ.get("RAGLEAP_RAG_PARITY_DIR", "/root/fixtures-rag-eval")
os.makedirs(OUT, exist_ok=True)
LINES = []


def H(x):
    return struct.pack(">d", float(x)).hex()


def L(xs):
    return "|".join(xs)


def rm(name, ranked, relevant, ks):
    try:
        r = E.retrieval_metrics(ranked, relevant, ks)
        res = "OK\t" + "|".join("%s=%s" % (k, H(v)) for k, v in r.items())
    except ValueError as e:
        res = "ERR\t" + str(e)
    LINES.append("\t".join(["RM", name, L(ranked), L(relevant), "|".join(str(k) for k in ks), res]))


def pc(name, values, q):
    try:
        res = "OK\t" + H(E.percentile(values, q))
    except ValueError as e:
        res = "ERR\t" + str(e).split(" got ", 1)[0]
    LINES.append("\t".join(["PC", name, "|".join(H(v) for v in values), repr(q), res]))


def sm(name, values, fail_below):
    r = E.summarize(values, fail_below)
    if r is None:
        res = "NONE\t"
    else:
        parts = ["n=%d" % r["n"], "mean=" + H(r["mean"]), "min=" + H(r["min"]), "p10=" + H(r["p10"]),
                 "p50=" + H(r["p50"]), "p95=" + H(r["p95"])]
        if "fail_rate" in r:
            parts.append("fail_rate=" + H(r["fail_rate"]))
        res = "OK\t" + "|".join(parts)
    LINES.append("\t".join(["SM", name, "|".join("N" if v is None else H(v) for v in values),
                            "N" if fail_below is None else repr(fail_below), res]))


D = list(E.DEFAULT_METRIC_KS)
POOL = ["d%02d.txt" % i for i in range(30)] + ["\u00e9-\u00fc.txt", "\U0001F600.md", "\u65e5\u672c\u8a9e.pdf"]
rm("basic_second", ["a", "b", "c"], ["b"], [1, 3])
rm("first_hit", ["a", "b", "c"], ["a"], D)
rm("no_hit", ["a", "b", "c"], ["z"], D)
rm("all_relevant", ["a", "b", "c"], ["a", "b", "c"], D)
rm("empty_ranked", [], ["a"], D)
rm("duplicates_count_once", ["a", "a", "b", "a", "b"], ["b"], D)
rm("relevant_duplicates", ["a", "b"], ["b", "b", "a"], D)
rm("fewer_than_k", ["a"], ["a", "b"], [10])
rm("k_larger_than_pool", ["a", "b"], ["b"], [50])
rm("unicode_names", ["\U0001F600.md", "\u00e9-\u00fc.txt"], ["\u00e9-\u00fc.txt"], D)
rm("duplicate_ks", ["a", "b"], ["b"], [2, 2, 1])
rm("unsorted_ks", ["a", "b", "c"], ["c"], [7, 3, 1])
rm("err_no_relevant", ["a"], [], D)
rm("err_no_ks", ["a"], ["a"], [])
rm("err_zero_k", ["a"], ["a"], [1, 0])
rm("err_negative_k", ["a"], ["a"], [-3])
rm("err_both_relevant_first", ["a"], [], [])
rng = random.Random(20261105)
KS = [D, [1], [2, 2], [20], [1, 3, 5, 10, 50], [7, 3], [40]]
for i in range(150):
    ranked = [rng.choice(POOL) for _ in range(rng.randint(0, 40))]
    relevant = [rng.choice(POOL + ["missing%d.txt" % j for j in range(3)]) for _ in range(rng.randint(1, 6))]
    rm("rand_%03d" % i, ranked, relevant, rng.choice(KS))

QS = [0, 1, 5, 10, 25, 33.3, 50, 66.6, 75, 90, 95, 99, 99.9, 100, 0.1, 1e-9, 12.5, 100.0, 0.0]
pc("three", [3.0, 1.0, 2.0], 50)
pc("four_q50", [1.0, 2.0, 3.0, 4.0], 50)
pc("four_q0", [1.0, 2.0, 3.0, 4.0], 0)
pc("four_q95", [1.0, 2.0, 3.0, 4.0], 95)
pc("single", [7.5], 95)
pc("negative_values", [-2.0, -1.0, 0.5], 10)
pc("err_empty", [], 50)
pc("err_q_negative", [1.0, 2.0], -0.1)
pc("err_q_big", [1.0, 2.0], 100.1)
pc("err_both_empty_first", [], -5)
for i in range(100):
    vals = [round(rng.uniform(0, 1), 3) if rng.random() < 0.8 else rng.choice([0.0, 1.0, 0.5]) for _ in range(rng.randint(1, 60))]
    pc("rand_%03d" % i, vals, rng.choice(QS))

sm("empty", [], None)
sm("all_none", [None, None], 0.5)
sm("one", [0.25], 0.5)
sm("with_none", [1.0, None, 0.0, 0.5], 0.5)
sm("no_fail_below", [0.1, 0.2, 0.3, 0.4], None)
sm("fail_below_zero", [0.0, 0.1], 0.0)
sm("fail_below_one", [0.0, 1.0, 0.5], 1.0)
for i in range(80):
    vals = [None if rng.random() < 0.15 else round(rng.uniform(0, 1), 3) for _ in range(rng.randint(0, 40))]
    sm("rand_%03d" % i, vals, rng.choice([None, 0.5, 0.0, 1.0, 0.25]))

# long lists: the 49th and 50th percentile only differ from 51 values up
for n in (51, 52, 99, 100, 101, 150, 200, 400):
    for i in range(3):
        vals = [None if rng.random() < 0.05 else round(rng.uniform(0, 1), 3) for _ in range(n)]
        sm("long_%d_%d" % (n, i), vals, rng.choice([None, 0.5]))
        pc("long_%d_%d" % (n, i), [v for v in vals if v is not None], rng.choice([10, 49, 50, 51, 95, 99]))

with open(os.path.join(OUT, "eval_metrics_cases.txt"), "w", encoding="utf-8", newline="\n") as f:
    f.write("\n".join(LINES) + "\n")
print("python:", sys.version.split()[0], "| RM:", sum(1 for x in LINES if x.startswith("RM")),
      "| PC:", sum(1 for x in LINES if x.startswith("PC")), "| SM:", sum(1 for x in LINES if x.startswith("SM")),
      "| error lines:", sum(1 for x in LINES if "\tERR\t" in x))
print("written to", OUT)
