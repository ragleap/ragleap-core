"""False accepts of StreamGate and Silero VAD on real recorded noise (Colab, CPU, no token). Not a detection benchmark.
Needs: pip install onnxruntime numpy ragleap-voice==0.3.5, and MUSAN extracted so that musan/noise/free-sound exists
(MUSAN: Snyder, Chen and Povey 2015, listed as CC BY 4.0 on openslr.org/17; acknowledge it if you publish results).
Only the free-sound folder is used: its LICENSE file states that all selected recordings are marked Public Domain on Free Sound.
The sound-bible folder (listed as Attribution 3.0) is NOT used. Each file is cut to its first NOISE_MAX_SECONDS (default 10) and files
shorter than 1 s are skipped. Conditions: as recorded; scaled to -23 dBFS RMS (StreamGate is a level gate, so level matters);
and the -23 dBFS version through ragleap's own 16 to 8 kHz path (down2, mu-law encode and decode, up2) for every NOISE_PHONE_EVERY-th file.
A trigger is a speech_start event (StreamGate) or three consecutive Silero windows with probability >= 0.5. On noise-only input a trigger
is a false accept. Some recordings may contain speech-like sounds: the script lists the accepted names so a person can listen to them.
Silero model: silero_vad_16k_op15.onnx at snakers4/silero-vad commit 066409f3d9db45a30d21dfcbf9147826ffdd7052, SHA-256 checked below.
Sound-library recordings, not noise recorded in a room or on a phone line. No speech is mixed in, so this says nothing about detecting speech."""
import array, csv, hashlib, json, math, os, sys, urllib.request, wave
import numpy as np, onnxruntime as ort
import ragleap_voice
from ragleap_voice.audio import down2, ulaw_decode, ulaw_encode, up2
from ragleap_voice.gate import StreamGate

DIR = os.environ.get("NOISE_DIR", "musan/noise/free-sound")
MAX_S = float(os.environ.get("NOISE_MAX_SECONDS", "10"))
PHONE_EVERY = int(os.environ.get("NOISE_PHONE_EVERY", "4"))
OUT = os.environ.get("NOISE_OUT", "gate_real_noise")
RATE, N, CTX = 16000, 512, 64
URL = "https://raw.githubusercontent.com/snakers4/silero-vad/066409f3d9db45a30d21dfcbf9147826ffdd7052/src/silero_vad/data/silero_vad_16k_op15.onnx"
raw = urllib.request.urlopen(URL).read()
assert hashlib.sha256(raw).hexdigest() == "7ed98ddbad84ccac4cd0aeb3099049280713df825c610a8ed34543318f1b2c49"
sess = ort.InferenceSession(raw, providers=["CPUExecutionProvider"])


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
    ev += g.finish()
    return any(e.kind == "speech_start" for e in ev)


def wilson(k, n, z=1.96):
    p = k / n; d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d; h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0, c - h), min(1, c + h)


def rms(x):
    return math.sqrt(float(np.mean(x * x))) + 1e-9


def lvl(x, dbfs):
    return np.clip(x / rms(x) * 32767 * 10 ** (dbfs / 20), -32768, 32767)


def phone(x):
    return np.asarray(up2(ulaw_decode(ulaw_encode(down2(to_arr(x))))), dtype=np.float64)


def load(path):
    with wave.open(path, "rb") as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (RATE, 1, 2):
            return None
        return np.frombuffer(w.readframes(min(w.getnframes(), int(MAX_S * RATE))), dtype="<i2").astype(np.float64)


DET = {"gate_default": lambda x: gate(x, None), "gate_floor_-60": lambda x: gate(x, {"floor_dbfs": -60.0}), "silero": silero}
names = sorted(f for f in os.listdir(DIR) if f.endswith(".wav"))
rows, skipped = [], {"short": 0, "format": 0}
for n, name in enumerate(names):
    x = load(os.path.join(DIR, name))
    if x is None:
        skipped["format"] += 1; continue
    if len(x) < RATE:
        skipped["short"] += 1; continue
    conds = {"recorded": x, "fixed_-23dBFS": lvl(x, -23.0)}
    if n % PHONE_EVERY == 0:
        conds["phone_fixed_-23dBFS"] = phone(conds["fixed_-23dBFS"])
    for c, sig in conds.items():
        for d, fn in DET.items():
            rows.append({"file": name, "condition": c, "detector": d, "accepted": int(bool(fn(sig)))})
    if (n + 1) % 100 == 0:
        print("done", n + 1, "of", len(names), flush=True)

env = {"ragleap_voice": ragleap_voice.__version__, "python": sys.version.split()[0], "files_listed": len(names), "skipped": skipped,
       "max_seconds": MAX_S, "phone_every": PHONE_EVERY, "runs": 1}
print(json.dumps(env))
print("%-22s %-15s %5s %9s %6s  %s" % ("condition", "detector", "files", "accepted", "rate", "95% interval (Wilson)"))
summary = {}
for c in ("recorded", "fixed_-23dBFS", "phone_fixed_-23dBFS"):
    for d in DET:
        sel = [r for r in rows if r["condition"] == c and r["detector"] == d]
        k = sum(r["accepted"] for r in sel)
        lo, hi = wilson(k, len(sel))
        summary["%s|%s" % (c, d)] = {"files": len(sel), "accepted": k, "low": lo, "high": hi}
        print("%-22s %-15s %5d %9d %5.1f%%  [%.3f, %.3f]" % (c, d, len(sel), k, 100 * k / len(sel), lo, hi))
acc = sorted({r["file"] for r in rows if r["detector"] == "silero" and r["accepted"] and r["condition"] == "fixed_-23dBFS"})
print("files Silero accepted at -23 dBFS (listen to these):", len(acc), acc[:40])
with open(OUT + "_files.csv", "w", newline="") as fh:
    wr = csv.DictWriter(fh, fieldnames=["file", "condition", "detector", "accepted"]); wr.writeheader(); wr.writerows(rows)
json.dump({"env": env, "summary": summary}, open(OUT + "_summary.json", "w"), indent=1)
print("saved", OUT + "_files.csv", OUT + "_summary.json")
print("Limits: sound-library noise, not room or phone-line noise; no speech mixed in, false accepts only; one run; first 10 s of each file; thresholds unmeasured.")
