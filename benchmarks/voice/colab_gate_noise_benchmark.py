"""StreamGate false accepts on noise, and detection on speech plus noise (Colab, CPU is enough, no token).
Run 2026-11-02, one run, seed 1. The gate only: no speech model is run. Needs: pip install ragleap-voice==0.3.2 datasets==3.6.0 numpy.
A trigger is a speech_start event. For noise-only inputs a trigger is a false accept. For speech plus noise it only shows the
outcome tracks the speech clip's own level; noise alone also triggers, so it does not show speech is separated from noise.
Public clips only: google/fleurs en_us test, first 30 clips. Synthetic noise is generated here."""
import array, math
import numpy as np
from datasets import load_dataset
from ragleap_voice.gate import StreamGate

RATE = 16000


def wilson(k, n, z=1.96):
    p = k / n; d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d; h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0, c - h), min(1, c + h)


def to_arr(x):
    a = array.array("h"); a.frombytes(np.clip(np.round(x), -32768, 32767).astype("<i2").tobytes()); return a


def triggered(x, opts):
    g = StreamGate(RATE, opts); a = to_arr(x); ev = []
    for i in range(0, len(a), 3200):
        ev += g.feed(a[i:i + 3200])
    ev += g.finish()
    return any(e.kind == "speech_start" for e in ev)


clips = []
for ex in load_dataset("google/fleurs", "en_us", split="test", streaming=True, trust_remote_code=True):
    clips.append(np.asarray(ex["audio"]["array"], dtype=np.float64) * 32767)
    if len(clips) == 30:
        break
rng = np.random.default_rng(1)
def white(dbfs, n): return rng.standard_normal(n) * 32767 * 10 ** (dbfs / 20)
def hum(dbfs, n):
    t = np.arange(n) / RATE
    return np.sqrt(2) * 32767 * 10 ** (dbfs / 20) * np.sin(2 * np.pi * 50 * t + rng.uniform(0, 6.28))
def rms(x): return math.sqrt(float(np.mean(x * x))) + 1e-9

configs = {"default floor -46.8": {}, "floor -60": {"floor_dbfs": -60}}


def row(label, make):
    out = []
    for name, o in configs.items():
        k = sum(triggered(make(i), o) for i in range(30)); lo, hi = wilson(k, 30)
        out.append("%s: %2d/30 [%.2f-%.2f]" % (name, k, lo, hi))
    print("%-28s %s" % (label, " | ".join(out)))


print("== NOISE ONLY (a trigger = false accept; lower is better) ==")
row("digital silence", lambda i: np.zeros(5 * RATE))
for d in (-60, -50, -40, -30, -20):
    row("white noise %d dBFS" % d, lambda i, d=d: white(d, 5 * RATE))
for d in (-40, -30):
    row("50 Hz hum %d dBFS" % d, lambda i, d=d: hum(d, 5 * RATE))
print("== SPEECH + WHITE NOISE (noise alone also triggers) ==")
row("clean speech", lambda i: clips[i])
for snr in (20, 10, 0):
    row("speech + noise SNR %d dB" % snr, lambda i, snr=snr: clips[i] + rng.standard_normal(len(clips[i])) * rms(clips[i]) * 10 ** (-snr / 20))
