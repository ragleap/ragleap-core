"""Silero VAD (ONNX) on noise and speech, same inputs and seed as colab_gate_noise_benchmark.py (Colab, CPU, no token).
Run 2026-11-03, one run, seed 1. Needs: pip install onnxruntime numpy datasets==3.6.0.
Model: silero_vad_16k_op15.onnx from snakers4/silero-vad at commit 066409f3d9db45a30d21dfcbf9147826ffdd7052, SHA-256 checked below.
A trigger is 3 consecutive 32 ms frames with probability >= 0.5 (both unmeasured starting points). Noise is synthetic (white, 50 Hz hum).
Nothing here adds Silero to the package or manifest."""
import hashlib, math, urllib.request
import numpy as np, onnxruntime as ort
from datasets import load_dataset

URL = "https://raw.githubusercontent.com/snakers4/silero-vad/066409f3d9db45a30d21dfcbf9147826ffdd7052/src/silero_vad/data/silero_vad_16k_op15.onnx"
raw = urllib.request.urlopen(URL).read()
assert hashlib.sha256(raw).hexdigest() == "7ed98ddbad84ccac4cd0aeb3099049280713df825c610a8ed34543318f1b2c49", "sha mismatch"
sess = ort.InferenceSession(raw, providers=["CPUExecutionProvider"])
RATE, N, CTX = 16000, 512, 64


def probs(x):
    x = (x / 32768.0).astype(np.float32)
    state = np.zeros((2, 1, 128), dtype=np.float32); ctx = np.zeros(CTX, dtype=np.float32); out = []
    for i in range(0, len(x) - N + 1, N):
        frame = np.concatenate([ctx, x[i:i + N]])[None, :]
        p, state = sess.run(None, {"input": frame, "state": state, "sr": np.array(16000, dtype=np.int64)})
        ctx = x[i + N - CTX:i + N]; out.append(float(p[0, 0]))
    return np.array(out)


def trig(x, thr=0.5, k=3):
    run = 0
    for v in probs(x):
        run = run + 1 if v >= thr else 0
        if run >= k:
            return True
    return False


def wilson(k, n, z=1.96):
    p = k / n; d = 1 + z * z / n; c = (p + z * z / (2 * n)) / d; h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0, c - h), min(1, c + h)


clips = []
for ex in load_dataset("google/fleurs", "en_us", split="test", streaming=True, trust_remote_code=True):
    clips.append(np.asarray(ex["audio"]["array"], dtype=np.float64) * 32767)
    if len(clips) == 30:
        break
sp, si = probs(clips[0]).max(), probs(np.zeros(5 * RATE)).max()
print("sanity: max prob on speech %.2f, on silence %.2f" % (sp, si))
assert sp > 0.5 and si < 0.2, "model input convention is wrong for this file"
rng = np.random.default_rng(1)
def white(d, n): return rng.standard_normal(n) * 32767 * 10 ** (d / 20)
def hum(d, n):
    t = np.arange(n) / RATE
    return np.sqrt(2) * 32767 * 10 ** (d / 20) * np.sin(2 * np.pi * 50 * t + rng.uniform(0, 6.28))
def rms(x): return math.sqrt(float(np.mean(x * x))) + 1e-9


def row(label, make):
    k = sum(trig(make(i)) for i in range(30)); lo, hi = wilson(k, 30)
    print("%-28s silero: %2d/30 [%.2f-%.2f]" % (label, k, lo, hi))


print("== NOISE ONLY (a trigger = false accept; lower is better) ==")
row("digital silence", lambda i: np.zeros(5 * RATE))
for d in (-60, -50, -40, -30, -20):
    row("white noise %d dBFS" % d, lambda i, d=d: white(d, 5 * RATE))
for d in (-40, -30):
    row("50 Hz hum %d dBFS" % d, lambda i, d=d: hum(d, 5 * RATE))
print("== SPEECH (a trigger = detected; higher is better) ==")
row("clean speech", lambda i: clips[i])
for snr in (20, 10, 0):
    row("speech + noise SNR %d dB" % snr, lambda i, snr=snr: clips[i] + rng.standard_normal(len(clips[i])) * rms(clips[i]) * 10 ** (-snr / 20))
