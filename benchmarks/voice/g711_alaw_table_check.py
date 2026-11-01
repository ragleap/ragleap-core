"""A-law check against ITU-T G.711 (1988) Tables 1a/1b (printed pages 3 and 4). Tests every convention below, picks none.
Table values hard-coded from the PDF page images; no G.191 code. Not a conformance claim."""
import array, audioop, struct, sys
from bisect import bisect_left, bisect_right
X = [0]
for w in [2]*32 + [4]*16 + [8]*16 + [16]*16 + [32]*16 + [64]*16 + [128]*16:
    X.append(X[-1] + w)
for n, v in ((1,2),(32,64),(33,68),(48,128),(49,136),(64,256),(65,272),(80,512),(81,544),(96,1024),(97,1088),(112,2048),(113,2176),(127,3968),(128,4096)):
    assert X[n] == v, (n, X[n], v)
Y = [(X[n] + X[n+1]) // 2 for n in range(128)]
PRINTED = ((0,128,1),(32,160,66),(48,176,132),(64,192,264),(80,208,528),(96,224,1056),(112,240,2112),(127,255,4032))
def dec(c):
    p = c ^ 0x55
    return 8 * Y[p - 128] if p >= 128 else -8 * Y[p]
for n, ch, y in PRINTED:
    assert Y[n] == y and ch == 128 + n and dec(ch ^ 0x55) == 8 * y and dec(n ^ 0x55) == -8 * y, n
print("printed Table 1a/1b vectors match (character signals, quantized values)")
CONV = {"sym_trunc": (lambda s: abs(s) // 8, 1), "signed_floor": (lambda s: abs(s >> 3), 1),
        "ones_compl": (lambda s: (abs(s) - 1) // 8 if s < 0 else s // 8, 1),
        "round": (lambda s: (abs(s) + 4) // 8, 1), "exact": (lambda s: abs(s), 8)}
def enc(s, conv, tie):
    f, k = CONV[conv]; xs = [k * x for x in X]; m = f(s)
    i = (bisect_right(xs, m) if tie == "up" else bisect_left(xs, m)) - 1
    i = max(0, min(127, i))
    return (i if s < 0 else 128 + i) ^ 0x55
S = range(-32768, 32768)
a_enc = audioop.lin2alaw(array.array("h", S).tobytes(), 2)
print("audioop:", audioop.__file__ if hasattr(audioop, "__file__") else "built-in")
a_dec = array.array("h"); a_dec.frombytes(audioop.alaw2lin(bytes(range(256)), 2))
print("decoder vs table (x8): audioop mismatches", sum(a_dec[c] != dec(c) for c in range(256)))
tabs = {}
print("\nconvention              audioop_mismatch")
for conv in CONV:
    for tie in ("up", "down"):
        t = [enc(s, conv, tie) for s in S]; tabs[(conv, tie)] = t
        print("%-22s %9d" % (conv + "/" + tie, sum(x != y for x, y in zip(t, a_enc))))
d1, d2 = tabs[("sym_trunc", "up")], tabs[("ones_compl", "up")]
diff = [s for s, x, y in zip(S, d1, d2) if x != y]
want = [-8 * X[n] for n in range(1, 128)]
print("\nsym_trunc/up vs ones_compl/up differ on", len(diff), "inputs; all negative decision values 8*x_n:", sorted(diff) == sorted(want))
print("sym_trunc/up == exact/up:", tabs[("sym_trunc", "up")] == tabs[("exact", "up")])
print("round trip enc(dec(c))==c for all 256 codes (sym_trunc/up):", all(enc(dec(c), "sym_trunc", "up") == c for c in range(256)))
