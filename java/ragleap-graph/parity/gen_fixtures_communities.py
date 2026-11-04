# Generates parity recordings for ragleap_graph.communities (detect_communities, modularity, members_hash) by running
# the real Python code. Writes communities_cases.json into RAGLEAP_GRAPH_PARITY_DIR (default /root/fixtures-graph-comm).
# Pinned commit: 30affe6ade7caaeb4b27f73cd0ac055fa204d52d (packages/ragleap-graph; communities.py is pure, standard library only).
# Export first: git archive 30affe6ade7caaeb4b27f73cd0ac055fa204d52d packages/ragleap-graph | tar -x -C /root/py-graph-comm-pinned
# Recorded with Python 3.11. From Python 3.12 sum() of floats is compensated and can differ in the last bit.
import itertools, json, math, os, random, sys
SRC = os.environ.get("RAGLEAP_GRAPH_SRC", "/root/py-graph-comm-pinned/packages/ragleap-graph/src/ragleap_graph")
sys.path.insert(0, SRC)
import communities as C
assert C.__file__.startswith(SRC), "wrong module path"
OUT = os.environ.get("RAGLEAP_GRAPH_PARITY_DIR", "/root/fixtures-graph-comm")
os.makedirs(OUT, exist_ok=True)


def enc(v):
    if isinstance(v, bool):
        return {"k": "bool", "v": v}
    if v is None:
        return {"k": "none"}
    if isinstance(v, str):
        return {"k": "str", "v": v}
    if isinstance(v, float) and math.isnan(v):
        return {"k": "nan"}
    if isinstance(v, float) and math.isinf(v):
        return {"k": "inf" if v > 0 else "ninf"}
    return {"k": "num", "v": v}


def java_ok(resolution, min_size):
    r_ok = resolution is not None and not isinstance(resolution, (bool, str))
    m_ok = isinstance(min_size, int) and not isinstance(min_size, bool)
    return r_ok and m_ok


DETECT = []
MOD = []


def add(name, edges, resolution=1.0, min_size=2):
    item = {"name": name, "edges": [[a, b, enc(w)] for a, b, w in edges], "resolution": enc(resolution),
            "min_size": enc(min_size), "java": java_ok(resolution, min_size)}
    try:
        item["result"] = C.detect_communities(edges, resolution=resolution, min_size=min_size)
        item["ok"] = True
        part = [c["members"] for c in C.detect_communities(edges, resolution=resolution, min_size=1)]
        item["partition"] = part
        item["modularity"] = C.modularity(edges, part, float(resolution))
    except ValueError as e:
        item["ok"] = False
        item["error_type"] = "ValueError"
        item["error_param"] = str(e).split(" ")[0]
    DETECT.append(item)
    return item


def addm(name, edges, communities, resolution=1.0):
    item = {"name": name, "edges": [[a, b, enc(w)] for a, b, w in edges], "communities": communities,
            "resolution": resolution}
    try:
        item["value"] = C.modularity(edges, communities, resolution)
        item["ok"] = True
    except ValueError:
        item["ok"] = False
    MOD.append(item)


def tri(p, w=1.0):
    return [(p + "1", p + "2", w), (p + "2", p + "3", w), (p + "1", p + "3", w)]


def ring(n, w=1.0):
    return [("n%d" % i, "n%d" % ((i + 1) % n), w) for i in range(n)]


def clique(names, w=1.0):
    return [(a, b, w) for a, b in itertools.combinations(names, 2)]


two_tri = tri("a") + tri("b") + [("a3", "b1", 1.0)]
extras = [("s", "s", 1.0), ("a1", "z", 0.0), ("a1", "z2", -1.0), ("a1", "z3", float("nan")),
          ("a1", "z4", float("inf")), ("a1", "z5", float("-inf")), ("a1", "z6", True),
          ("a1", "z7", "2"), ("a1", "z8", None)]
dup_float = [("a", "b", 0.1), ("b", "a", 0.2), ("a", "b", 0.3), ("b", "c", 0.7), ("c", "b", 0.1),
             ("a", "c", 1e-9), ("c", "d", 0.3), ("d", "c", 0.6), ("b", "d", 0.1)]
U = ["\uffff", "\U00010000", "\U0001F600", "\ue000", "a", "\u00e9", "\ud7ff", "\uff5e", "\U0010ffff", "Z"]
uni = clique(U[:5]) + clique(U[5:]) + [(U[0], U[5], 0.5)]

add("empty", [])
add("single_edge", [("a", "b", 1.0)])
add("single_edge_min1", [("a", "b", 1.0)], min_size=1)
add("triangle", tri("t"))
add("two_triangles_bridge", two_tri)
add("two_triangles_disconnected", tri("a") + tri("b"))
add("ring4", ring(4)); add("ring6", ring(6)); add("ring7", ring(7))
add("path5", [("n%d" % i, "n%d" % (i + 1), 1.0) for i in range(4)])
add("path4", [("n%d" % i, "n%d" % (i + 1), 1.0) for i in range(3)])
add("star6", [("hub", "leaf%d" % i, 1.0) for i in range(6)])
add("k4", clique(["a", "b", "c", "d"])); add("k5", clique(["a", "b", "c", "d", "e"]))
add("k4_weighted", [(a, b, float(1 + i)) for i, (a, b) in enumerate(itertools.combinations("abcd", 2))])
add("two_pairs_tie", [("a", "b", 1.0), ("c", "d", 1.0)])
add("square_diagonal", [("a", "b", 1.0), ("b", "c", 1.0), ("c", "d", 1.0), ("d", "a", 1.0), ("a", "c", 1.0)])
for res in (0.01, 0.5, 1, 1.5, 2, 5, 50):
    add("two_tri_bridge_res_%s" % res, two_tri, resolution=res)
    add("uni_res_%s" % res, uni, resolution=res, min_size=1)
small = two_tri + [("x", "y", 1.0), ("p", "q", 2.0), ("q", "r", 2.0)]
for ms in (1, 2, 3, 4, 100):
    add("small_components_min_%d" % ms, small, min_size=ms)
add("invalid_edges_ignored", two_tri + extras)
add("only_invalid_edges", extras)
add("int_weights", [("a", "b", 2), ("b", "c", 3), ("a", "c", 1), ("c", "d", 1)])
add("duplicates_float_sum", dup_float, min_size=1)
add("duplicates_float_sum_reversed", list(reversed(dup_float)), min_size=1)
add("tiny_and_huge_weights", [("a", "b", 1e-9), ("b", "c", 1e12), ("a", "c", 1e-9), ("c", "d", 5e11), ("d", "e", 1e-9)], min_size=1)
add("unicode_names", uni, min_size=1)
add("unicode_names_reversed", list(reversed(uni)), min_size=1)
add("utf16_trap_pair", [("\uffff", "\U00010000", 1.0), ("\U00010000", "\U0001F600", 1.0), ("\uffff", "\U0001F600", 1.0)], min_size=1)
for bad in (0, -1, float("nan"), float("inf"), float("-inf"), True, "1", None):
    add("bad_resolution_%s" % repr(bad), two_tri, resolution=bad)
for bad in (0, -3, True, 1.5, "2", None, 2.0):
    add("bad_min_size_%s" % repr(bad), two_tri, min_size=bad)
add("both_bad_resolution_first", two_tri, resolution=0, min_size=0)

POOL = ["n%02d" % i for i in range(60)] + U


def rand_graph(seed, n, p, kind):
    rng = random.Random(seed)
    names = rng.sample(POOL, n)
    edges = []
    for i in range(n):
        for j in range(i + 1, n):
            if rng.random() < p:
                w = rng.randint(1, 5) if kind == "int" else (round(rng.uniform(0.01, 3.0), 3) if kind == "float" else 1.0)
                a, b = names[i], names[j]
                if rng.random() < 0.5:
                    a, b = b, a
                edges.append((a, b, w))
                if rng.random() < 0.1:
                    edges.append((b, a, round(rng.uniform(0.01, 2.0), 3)))
    rng.shuffle(edges)
    return edges


mismatch = 0
sizes = [8, 12, 20, 30, 40, 60]
kinds = ["int", "float", "unit"]
ress = [1.0, 1.0, 0.7, 1.0, 2.0, 1.0]
for seed in range(1, 25):
    n = sizes[seed % len(sizes)]
    edges = rand_graph(seed, n, 0.08 + 0.02 * (seed % 10), kinds[seed % 3])
    res = ress[seed % len(ress)]
    ms = 1 if seed % 4 == 0 else 2
    a = add("random_%02d" % seed, edges, resolution=res, min_size=ms)
    shuffled = list(edges)
    random.Random(seed + 1000).shuffle(shuffled)
    shuffled = [(b, a_, w) if random.Random(seed + i).random() < 0.5 else (a_, b, w) for i, (a_, b, w) in enumerate(shuffled)]
    b = add("random_%02d_shuffled" % seed, shuffled, resolution=res, min_size=ms)
    if a["result"] != b["result"]:
        mismatch += 1

# near ties: a node linked to two equal cliques by weights that differ by a tiny delta (below and above EPSILON)
def two_cliques_x(xname, delta, swap):
    wa, wb = (1.0 + delta, 1.0) if swap else (1.0, 1.0 + delta)
    return clique(["a1", "a2", "a3"]) + clique(["b1", "b2", "b3"]) + [(xname, "a1", wa), (xname, "b1", wb)]


for xname in ("x", "0"):
    for swap in (False, True):
        for delta in (0.0, 1e-13, 1e-11, 1e-9, 1e-6, 1e-4, 5e-4, 2e-3):
            add("near_tie_%s_%s_%s" % (xname, swap, delta), two_cliques_x(xname, delta, swap), min_size=1)
for n in (5, 6, 8):
    for delta in (1e-13, 1e-9, 1e-6, 1e-4, 1e-2):
        add("near_tie_ring%d_%s" % (n, delta), [("n%d" % i, "n%d" % ((i + 1) % n), 1.0 + i * delta) for i in range(n)], min_size=1)

# float sums over names where UTF-16 order and code point order differ (BMP high characters vs astral ones)
FW = [0.1, 0.2, 0.3, 0.7, 0.35, 1e-9, 0.15]
for seed in range(1, 13):
    rng = random.Random(900 + seed)
    names = rng.sample(U, 6 if seed % 2 else 8)
    es = []
    for a, b in itertools.combinations(names, 2):
        if rng.random() < 0.7:
            es.append((a, b, rng.choice(FW)))
            if rng.random() < 0.4:
                es.append((b, a, rng.choice(FW)))
    add("utf16_float_%02d" % seed, es, min_size=1)

nodes = sorted({x for a, b, w in two_tri for x in (a, b)})
addm("mod_one_community", two_tri, [nodes])
addm("mod_singletons", two_tri, [[x] for x in nodes])
addm("mod_two_triangles", two_tri, [["a1", "a2", "a3"], ["b1", "b2", "b3"]])
addm("mod_missing_node", two_tri, [["a1", "a2", "a3"], ["b1", "b2"]])
for r in (0.0, 0.5, 2.0, -1.5):
    addm("mod_res_%s" % r, two_tri, [["a1", "a2", "a3"], ["b1", "b2", "b3"]], resolution=r)
addm("mod_no_edges", [], [])
addm("mod_only_invalid_edges", extras, [])
addm("mod_extra_nodes", two_tri, [["a1", "a2", "a3", "ghost"], ["b1", "b2", "b3", "ghost2"]])
addm("mod_node_in_two", two_tri, [["a1", "a2", "a3", "b1"], ["b1", "b2", "b3"]])
addm("mod_float_weights", dup_float, [["a", "b", "c"], ["d"]])
addm("mod_float_weights_split", dup_float, [["a", "b"], ["c", "d"]])
addm("mod_unicode", uni, [U[:5], U[5:]])

json.dump({"python": sys.version.split()[0], "pin": os.environ.get("RAGLEAP_PIN"), "detect": DETECT, "modularity": MOD},
          open(os.path.join(OUT, "communities_cases.json"), "w"), allow_nan=False)
ok = [d for d in DETECT if d["ok"]]
print("python:", sys.version.split()[0], "| detect scenarios:", len(DETECT), "| ok:", len(ok), "| errors:", len(DETECT) - len(ok),
      "| java-eligible:", sum(1 for d in DETECT if d["java"]), "| modularity cases:", len(MOD),
      "(errors: %d)" % sum(1 for m in MOD if not m["ok"]))
print("communities recorded:", sum(len(d["result"]) for d in ok), "| python input-order mismatches (must be 0):", mismatch)
print("written to", OUT)
