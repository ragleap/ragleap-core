# Generates parity recordings for ragleap.evaluation.evaluate (per-case scoring, support checker, summary) and for
# compare_runs on two evaluate() results, by running the real Python code on scripted fake answers. perf_counter is
# replaced by recorded readings so latency is deterministic. Writes eval_run_cases.json into RAGLEAP_RAG_PARITY_DIR
# (default /root/fixtures-rag-eval). Every float is the string "h:" + its 16-digit hex bit pattern.
# Pinned commit: 94575d990d89b0e328bffdc14f4fa845d95b0153 (packages/ragleap-rag, evaluation.py is standard library only).
# Export first: git archive 94575d990d89b0e328bffdc14f4fa845d95b0153 packages/ragleap-rag/src/ragleap/evaluation.py | tar -x -C /root/py-rag-eval-pinned
import copy, importlib.util, json, os, random, struct, sys, time, types
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


WORDS = ["alpha", "Beta", "GAMMA", "delta", "\u00c9lan", "omega", "zeta", "kappa"]
DOCS = ["d%02d.txt" % i for i in range(8)] + ["\u00e9.txt", "\U0001F600.md"]
rng = random.Random(20261107)


class Rag:
    def __init__(self, answers):
        self.answers = answers

    def ask(self, query, **kw):
        return copy.deepcopy(self.answers[query])


def rand_answer():
    docs = [rng.choice(DOCS) for _ in range(rng.randint(0, 5))]
    cits = []
    for d in docs:
        name = d if rng.random() > 0.1 else rng.choice([None, ""])
        cits.append({"document_name": name, "text_preview": " ".join(rng.sample(WORDS, rng.randint(0, 3)))})
    sources = list(dict.fromkeys(docs))
    if rng.random() < 0.3:
        rng.shuffle(sources)
    text = " ".join(rng.choice([w, w.upper(), w.lower()]) for w in rng.sample(WORDS, rng.randint(0, 5)))
    usage = rng.choice([None, {"prompt_tokens": rng.randint(0, 500), "completion_tokens": rng.randint(0, 200),
                               "total_tokens": rng.randint(0, 700)}])
    return {"answer": text, "sources": sources, "citations": cits, "usage": usage}


def rand_cases(n):
    return [{"query": "q%d" % i, "expected_document": rng.choice([None] + DOCS),
             "expected_keywords": rng.sample(WORDS, rng.randint(0, 3)),
             "relevant_documents": [rng.choice(DOCS) for _ in range(rng.randint(0, 3))]} for i in range(n)]


def rand_clock(n):
    t = rng.uniform(0, 50)
    out = []
    for _ in range(n):
        out.append(t)
        t += rng.uniform(0.001, 3.0)
        out.append(t)
        t += rng.uniform(0, 0.5)
    return out


OPTS = [["ok", 0.0], ["ok", 1.0], ["ok", 0.25], ["ok", 0.9], ["ok", 0.5], ["ok", 0.8], ["raise"], ["bad", 1.5], ["bad", -0.5], ["bad", 2.0]]


def rand_script(n):
    return [rng.choice(OPTS) for _ in range(n)]


def do_run(cases, answers, readings, ks, fb, script):
    it = iter(readings)
    E.time = types.SimpleNamespace(perf_counter=lambda: next(it))
    inputs = []
    checker = None
    if script is not None:
        sit = iter(script)

        def checker(text, cits):
            inputs.append([text, [[c.get("document_name"), c.get("text_preview")] for c in cits]])
            k = next(sit)
            if k[0] == "raise":
                raise RuntimeError("boom")
            return k[1]
    try:
        run = E.evaluate(Rag(answers), cases, metric_ks=ks, support_checker=checker, fail_below=fb)
    except ValueError as e:
        run = {"error": str(e)}
    finally:
        E.time = time
    return run, inputs


def cmp_enc(c):
    lat = c["latency_s"]
    return {"metrics": {k: enc(v["delta"]) for k, v in c["metrics"].items()}, "regressions": c["regressions"],
            "improvements": c["improvements"],
            "changes": ["%s|%s|%s" % (x["query"], x["metric"], x["kind"]) for x in c["case_changes"]],
            "has_regressions": c["has_regressions"],
            "latency": None if lat is None else [enc(lat[k]) for k in ("baseline_mean", "candidate_mean", "baseline_p95", "candidate_p95")]}


SC = []
KS = [[1, 3, 5, 10], [1], [2, 2], [1, 3, 5, 10, 50], [7, 3]]
FBS = [0.5, 0.0, 1.0, 0.25]


def add(name, cases, ks=None, fb=0.5, script="auto", pair=False, tol=0.0, answers=None):
    ks = ks if ks is not None else rng.choice(KS)
    answers = answers if answers is not None else {c["query"]: rand_answer() for c in cases}
    if script == "auto":
        script = rand_script(len(cases)) if rng.random() < 0.6 else None
    if pair:
        script = None
    readings = rand_clock(len(cases))
    run, inputs = do_run(cases, answers, readings, ks, fb, script)
    sc = {"name": name, "cases": cases, "answers": enc(answers), "clock": enc(readings), "ks": ks, "fail_below": enc(fb),
          "checker": enc(script), "out": enc(run), "checker_inputs": inputs}
    if pair:
        answers_b = {c["query"]: rand_answer() for c in cases}
        readings_b = rand_clock(len(cases))
        run_b, _ = do_run(cases, answers_b, readings_b, ks, fb, None)
        sc["answers_b"] = enc(answers_b)
        sc["clock_b"] = enc(readings_b)
        sc["tolerance"] = enc(tol)
        sc["cmp"] = cmp_enc(E.compare_runs(run, run_b, tol))
    SC.append(sc)


add("err_no_cases", [], script=None)
add("err_zero_k", rand_cases(2), ks=[1, 0], script=None)
add("err_no_ks", rand_cases(2), ks=[], script=None)
add("err_fail_below_high", rand_cases(2), fb=1.5, script=None)
add("err_fail_below_negative", rand_cases(2), fb=-0.5, script=None)
add("err_ks_before_fail_below", rand_cases(2), ks=[0], fb=2.0, script=None)
for i in range(110):
    add("rand_%03d" % i, rand_cases(rng.randint(1, 8)), fb=rng.choice(FBS))
for i in range(45):
    add("pair_%03d" % i, rand_cases(rng.randint(1, 8)), fb=rng.choice(FBS), pair=True, tol=rng.choice([0.0, 0.0, 0.05, 0.2]))
json.dump({"python": sys.version.split()[0], "pin": os.environ.get("RAGLEAP_PIN"), "scenarios": SC},
          open(os.path.join(OUT, "eval_run_cases.json"), "w"), allow_nan=False)
errs = sum(1 for s in SC if "error" in s["out"])
print("python:", sys.version.split()[0], "| scenarios:", len(SC), "| errors:", errs, "| pairs:", sum(1 for s in SC if "cmp" in s),
      "| with checker:", sum(1 for s in SC if s["checker"] is not None), "| support errors:",
      sum(s["out"].get("summary", {}).get("support_errors", 0) for s in SC),
      "| cmp regressions:", sum(1 for s in SC if s.get("cmp", {}).get("has_regressions")))
print("written to", OUT)
