"""Silero VAD (ONNX) vs StreamGate on more synthetic noise and a 16k->8k->16k path (Colab, CPU, no token).
Run 2026-11-03, one run, seed 1. Needs: pip install onnxruntime numpy datasets==3.6.0 ragleap-voice==0.3.2.
Model: silero_vad_16k_op15.onnx at snakers4/silero-vad commit 066409f3d9db45a30d21dfcbf9147826ffdd7052, SHA-256 checked below.
Noise is synthetic. Babble is built from three other FLEURS clips and is the SAME input in all 30 trials (the function ignores
the trial index). The 8 kHz path is a resample with Down2Stream/Up2Stream, not a G.711 codec."""
import array, hashlib, math, urllib.request
import numpy as np, onnxruntime as ort
from datasets import load_dataset
from ragleap_voice.audio import Down2Stream, Up2Stream
from ragleap_voice.gate import StreamGate

URL = "https://raw.githubusercontent.com/snakers4/silero-vad/066409f3d9db45a30d21dfcbf9147826ffdd7052/src/silero_vad/data/silero_vad_16k_op15.onnx"
raw = urllib.request.urlopen(URL).read()
assert hashlib.sha256(raw).hexdigest() == "7ed98ddbad84ccac4cd0aeb3099049280713df825c610a8ed34543318f1b2c49"
sess = ort.InferenceSession(raw, providers=["CPUExecutionProvider"])
RATE, N, CTX = 16000, 512, 64


def probs(x):
    x = (x / 32768.0).astype(np.float32); st = np.zeros((2, 1, 128), np.float32); ctx = np.zeros(CTX, np.float32); out = []
    for i in range(0, len(x) - N + 1, N):
        p, st = sess.run(None, {"input": np.concatenate([ctx, x[i:i + N]])[None, :], "state": st, "sr": np.array(16000, np.int64)})
        ctx = x[i + N - CTX:i + N]; out.append(float(p[0, 0]))
    return np.array(out)


def silero(x, thr=0.5, k=3):
    run = 0
    for v in probs(x):
        run = run + 1 if v >= thr else 0
        if run >= k:
            return True
    return False


def to_arr(x):
    a = array.array("h"); a.frombytes(np.clip(np.round(x), -32768, 32767).astype("<i2").tobytes()); return a


def gate(x, opts):
    g = StreamGate(RATE, opts); a = to_arr(x); ev = []
    for i in range(0, len(a), 3200):
        ev += g.feed(a[i:i + 3200])
    ev += g.finish(); return any(e.kind == "speech_start" for e in ev)


def phone(x):
    d = Down2Stream(); u = Up2Stream(); a = to_arr(x)
    m = d.process(a); m.extend(d.finish()); o = u.process(m); o.extend(u.finish())
    return np.asarray(o, dtype=np.float64)


def wilson(k, n, z=1.96):
    p = k / n; dd = 1 + z * z / n; c = (p + z * z / (2 * n)) / dd; h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / dd
    return max(0, c - h), min(1, c + h)


clips = []
for ex in load_dataset("google/fleurs", "en_us", split="test", streaming=True, trust_remote_code=True):
    clips.append(np.asarray(ex["audio"]["array"], dtype=np.float64) * 32767)
    if len(clips) == 33:
        break
rng = np.random.default_rng(1)
def rms(x): return math.sqrt(float(np.mean(x * x))) + 1e-9
def pink(n):
    f = np.fft.rfft(rng.standard_normal(n)); f[1:] /= np.sqrt(np.arange(1, len(f))); x = np.fft.irfft(f, n); return x / rms(x)
def bandlim(n):
    f = np.fft.rfft(rng.standard_normal(n)); fr = np.fft.rfftfreq(n, 1 / RATE); f[(fr < 300) | (fr > 3400)] = 0
    x = np.fft.irfft(f, n); return x / rms(x)
def clipped(n): return np.clip(rng.standard_normal(n) * 3, -1, 1) / 0.9
def babble(i, n):
    mix = sum(np.resize(clips[30 + j], n) / rms(clips[30 + j]) for j in range(3)); return mix / rms(mix)
def lvl(x, dbfs): return x / rms(x) * 32767 * 10 ** (dbfs / 20)


cfg = {"gate default": lambda x: gate(x, {}), "gate -60": lambda x: gate(x, {"floor_dbfs": -60}), "silero": silero}


def row(label, make):
    parts = []
    for name, f in cfg.items():
        k = sum(f(make(i)) for i in range(30)); lo, hi = wilson(k, 30); parts.append("%s %2d/30 [%.2f-%.2f]" % (name, k, lo, hi))
    print("%-34s %s" % (label, " | ".join(parts)))


n = 5 * RATE
print("== NOISE ONLY, 16 kHz (trigger = false accept; babble is speech) ==")
for d in (-50, -35, -20):
    row("pink %d dBFS" % d, lambda i, d=d: lvl(pink(n), d))
    row("band-limited 300-3400 %d dBFS" % d, lambda i, d=d: lvl(bandlim(n), d))
    row("clipped white %d dBFS" % d, lambda i, d=d: lvl(clipped(n), d))
row("babble 3 speakers -30 dBFS", lambda i: lvl(babble(i, n), -30))
print("== SAME INPUTS AFTER 16k->8k->16k ==")
for d in (-35, -20):
    row("pink %d dBFS, 8k path" % d, lambda i, d=d: phone(lvl(pink(n), d)))
    row("band-limited %d dBFS, 8k path" % d, lambda i, d=d: phone(lvl(bandlim(n), d)))
row("clean speech, 8k path (detected)", lambda i: phone(clips[i]))
row("clean speech, 16k (detected)", lambda i: clips[i])
