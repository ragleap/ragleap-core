"""Effect of the 3:1 resamplers on Whisper large-v3 word error (Colab GPU): clean vs a 48 kHz round trip vs an 8 kHz round trip.
Needs: ragleap-voice from the main branch (down3 and up3 are not in a PyPI release yet), for example
pip install "ragleap-voice[whisper] @ git+https://github.com/ragleap/ragleap-core.git#subdirectory=packages/ragleap-voice"
plus datasets==3.6.0 numpy nvidia-cublas-cu12 nvidia-cudnn-cu12, and the model already pulled. Set R3_COMMIT to the commit installed.
No Hugging Face token should be set. Data: google/fleurs en_us, es_419, ru_ru test (CC-BY-4.0), the first R3_CLIPS (default 30) clips each.
round_48k: up3 (16 to 48 kHz) then down3 (48 to 16 kHz). round_8k: down2 then up2, no codec, as a reference for the 2:1 pair.
Both are pure Python filters on the clean clip: no Opus, no codec, no packet loss, not real LiveKit audio. The question is only whether our
filters change word error. Word error only. One run, 30 clips per language, wide intervals. No comparison with any other product."""
import array, csv, ctypes, glob, io, json, os, site, sys, time, wave
import numpy as np
from datasets import load_dataset

if os.environ.get("HF_TOKEN"):
    sys.exit("HF_TOKEN is set: delete it before running model code.")
for sp in site.getsitepackages():
    for pat in ("nvidia/cublas/lib/libcublas.so.12*", "nvidia/cudnn/lib/libcudnn*.so*"):
        for p in sorted(glob.glob(os.path.join(sp, pat))):
            try:
                ctypes.CDLL(p, mode=ctypes.RTLD_GLOBAL)
            except OSError:
                pass

import ragleap_voice
from ragleap_voice import Transcriber
try:
    from ragleap_voice.audio import down2, down3, up2, up3
except ImportError:
    sys.exit("down3 and up3 are missing: install ragleap-voice from the main branch, not from PyPI.")
from ragleap_voice.bench.metrics import normalize

LANGS = (("English", "en_us", "en"), ("Spanish", "es_419", "es"), ("Russian", "ru_ru", "ru"))
N = int(os.environ.get("R3_CLIPS", "30"))
OPTS = {"device": os.environ.get("R3_DEVICE", "auto")}
OUT = os.environ.get("R3_OUT", "resample3")
COND = ("clean", "round_48k", "round_8k")
RATE = 16000


def to_wav(a):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE); w.writeframes(a.tobytes())
    return buf.getvalue()


def snr_db(x, y):
    n = min(len(x), len(y)); x = np.asarray(x[:n], float); y = np.asarray(y[:n], float)
    return 10 * np.log10((x ** 2).sum() / max(((x - y) ** 2).sum(), 1e-9))


def edits(ref, hyp):
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i]
        for j, h in enumerate(hyp, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h)))
        prev = cur
    return prev[-1]


def load_clips(cfg):
    clips = []
    for ex in load_dataset("google/fleurs", cfg, split="test", streaming=True, trust_remote_code=True):
        x = np.asarray(ex["audio"]["array"], dtype=np.float64)
        pcm = array.array("h", np.clip(np.round(x * 32767), -32768, 32767).astype("<i2").tolist())
        clips.append((pcm, ex.get("raw_transcription") or ex["transcription"]))
        if len(clips) == N:
            break
    return clips


# the round trips must keep the clip intact before any WER is trusted
t0 = time.time()
first = load_clips("en_us")[0][0]
r48 = down3(up3(first)); r8 = up2(down2(first))
assert len(r48) == len(first), "48 kHz round trip changed the length: stop"
for name, r in (("round_48k", r48), ("round_8k", r8)):
    s = snr_db(first, r)
    print("self-check %s: SNR %.1f dB against the clean clip" % (name, s))
    assert s > 10, "round trip is broken: stop"
print("both round trips, one clip: %.1f s" % (time.time() - t0))

rng = np.random.default_rng(1)
B = 2000
rows, table = [], {}
with Transcriber() as tr:
    for lang, cfg, code in LANGS:
        clips = load_clips(cfg)
        E = {c: [] for c in COND}; W = []
        for pcm, text in clips:
            ref = normalize(text).split(); W.append(len(ref))
            audios = {"clean": pcm, "round_48k": down3(up3(pcm)), "round_8k": up2(down2(pcm))}
            for c in COND:
                hyp = normalize(tr.transcribe(to_wav(audios[c]), language=code, allow_unverified=True, options=OPTS).text).split()
                e = edits(ref, hyp); E[c].append(e)
                rows.append({"language": lang, "condition": c, "clip": len(W) - 1, "errors": e, "words": len(ref)})
        Wn = np.array(W, float); idx = rng.integers(0, len(W), (B, len(W))); den = Wn[idx].sum(1)
        En = {c: np.array(E[c], float) for c in COND}
        for c in COND:
            w = En[c][idx].sum(1) / den * 100
            d = (En[c][idx].sum(1) - En["clean"][idx].sum(1)) / den * 100
            table[(lang, c)] = {"wer": En[c].sum() / Wn.sum() * 100, "ci": np.percentile(w, [2.5, 97.5]).tolist(),
                                "vs_clean": [(En[c].sum() - En["clean"].sum()) / Wn.sum() * 100] + np.percentile(d, [2.5, 97.5]).tolist()}

env = {"ragleap_voice": ragleap_voice.__version__, "commit": os.environ.get("R3_COMMIT", "unrecorded"),
       "python": sys.version.split()[0], "clips_per_language": N, "device_option": OPTS["device"], "runs": 1}
print(json.dumps(env))
print("%-8s %-10s %-22s %-26s" % ("lang", "condition", "WER % (95% interval)", "change vs clean (points)"))
for (lang, c), r in table.items():
    print("%-8s %-10s %5.1f (%4.1f to %4.1f)   %+5.1f [%+5.1f, %+5.1f]" % ((lang, c, r["wer"]) + tuple(r["ci"]) + tuple(r["vs_clean"])))
with open(OUT + "_clips.csv", "w", newline="") as fh:
    wr = csv.DictWriter(fh, fieldnames=["language", "condition", "clip", "errors", "words"]); wr.writeheader(); wr.writerows(rows)
json.dump({"env": env, "table": {"%s|%s" % k: v for k, v in table.items()}}, open(OUT + "_summary.json", "w"), indent=1)
print("saved", OUT + "_clips.csv", OUT + "_summary.json")
print("Limits: filters only, no codec, no Opus, not real LiveKit audio; one run, 30 clips per language, wide intervals; word error only; no comparison with other systems.")
