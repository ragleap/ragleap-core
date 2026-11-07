"""Codec and resample effect on Whisper large-v3 word error: clean vs ffmpeg mu-law vs ragleap mu-law vs ragleap A-law (Colab GPU).
Needs: pip install "ragleap-voice[whisper]" datasets==3.6.0 numpy nvidia-cublas-cu12 nvidia-cudnn-cu12, ffmpeg, and the model already pulled.
No Hugging Face token should be set. Data: google/fleurs en_us, es_419, ru_ru test (CC-BY-4.0), the first PHONE_CLIPS (default 30) clips each.
ragleap path: down2 (16 to 8 kHz), encode, decode, up2 (8 to 16 kHz), all pure Python, so the CPU part takes a while.
ffmpeg path: 8 kHz pcm_mulaw and back to 16 kHz, as in colab_noise_pilot.py. No level normalisation and no added noise, so absolute WER is
comparable only within this run, not with the noise pilot. Word error only. A resample plus a codec is not a real phone network:
no packet loss, jitter or carrier processing. One run, 30 clips per language, wide intervals. No comparison with any other product."""
import array, csv, ctypes, glob, io, json, os, site, subprocess, sys, tempfile, time, wave
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
from ragleap_voice.audio import alaw_decode, alaw_encode, down2, ulaw_decode, ulaw_encode, up2
from ragleap_voice.bench.metrics import normalize

LANGS = (("English", "en_us", "en"), ("Spanish", "es_419", "es"), ("Russian", "ru_ru", "ru"))
N = int(os.environ.get("PHONE_CLIPS", "30"))
OPTS = {"device": os.environ.get("PHONE_DEVICE", "auto")}
OUT = os.environ.get("PHONE_OUT", "phone_line")
COND = ("clean", "ffmpeg_mulaw", "ragleap_mulaw", "ragleap_alaw")
RATE = 16000


def to_wav(a):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE); w.writeframes(a.tobytes())
    return buf.getvalue()


def ffmpeg_mulaw(a, tmp):
    p = lambda n: os.path.join(tmp, n)
    with open(p("a.wav"), "wb") as fh:
        fh.write(to_wav(a))
    for cmd in (["ffmpeg", "-y", "-loglevel", "error", "-i", p("a.wav"), "-ar", "8000", "-ac", "1", "-c:a", "pcm_mulaw", p("b.wav")],
                ["ffmpeg", "-y", "-loglevel", "error", "-i", p("b.wav"), "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", p("c.wav")]):
        subprocess.run(cmd, check=True)
    with wave.open(p("c.wav"), "rb") as w:
        assert w.getframerate() == RATE
        out = array.array("h"); out.frombytes(w.readframes(w.getnframes()))
    return out


def ragleap_path(a, enc, dec):
    return up2(dec(enc(down2(a))))


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


# the codec paths must round-trip before any WER is trusted
t0 = time.time()
first = load_clips("en_us")[0][0]
low = down2(first)
for name, enc, dec in (("mulaw", ulaw_encode, ulaw_decode), ("alaw", alaw_encode, alaw_decode)):
    s = snr_db(low, dec(enc(low)))
    print("codec self-check %s: SNR %.1f dB on the 8 kHz signal" % (name, s))
    assert s > 20, "codec round trip is broken: stop"
print("resample plus codec, one clip: %.1f s" % (time.time() - t0))

rng = np.random.default_rng(1)
B = 2000
rows, table = [], {}
with Transcriber() as tr, tempfile.TemporaryDirectory() as tmp:
    for lang, cfg, code in LANGS:
        clips = load_clips(cfg)
        E = {c: [] for c in COND}; W = []
        for pcm, text in clips:
            ref = normalize(text).split(); W.append(len(ref))
            audios = {"clean": pcm, "ffmpeg_mulaw": ffmpeg_mulaw(pcm, tmp),
                      "ragleap_mulaw": ragleap_path(pcm, ulaw_encode, ulaw_decode),
                      "ragleap_alaw": ragleap_path(pcm, alaw_encode, alaw_decode)}
            for c in COND:
                hyp = normalize(tr.transcribe(to_wav(audios[c]), language=code, allow_unverified=True, options=OPTS).text).split()
                e = edits(ref, hyp); E[c].append(e)
                rows.append({"language": lang, "condition": c, "clip": len(W) - 1, "errors": e, "words": len(ref)})
        Wn = np.array(W, float); idx = rng.integers(0, len(W), (B, len(W))); den = Wn[idx].sum(1)
        En = {c: np.array(E[c], float) for c in COND}
        for c in COND:
            w = En[c][idx].sum(1) / den * 100
            d = (En[c][idx].sum(1) - En["clean"][idx].sum(1)) / den * 100
            d2 = (En[c][idx].sum(1) - En["ffmpeg_mulaw"][idx].sum(1)) / den * 100
            table[(lang, c)] = {"wer": En[c].sum() / Wn.sum() * 100, "ci": np.percentile(w, [2.5, 97.5]).tolist(),
                                "vs_clean": [(En[c].sum() - En["clean"].sum()) / Wn.sum() * 100] + np.percentile(d, [2.5, 97.5]).tolist(),
                                "vs_ffmpeg": [(En[c].sum() - En["ffmpeg_mulaw"].sum()) / Wn.sum() * 100] + np.percentile(d2, [2.5, 97.5]).tolist()}

env = {"ragleap_voice": ragleap_voice.__version__, "python": sys.version.split()[0], "clips_per_language": N, "device_option": OPTS["device"], "runs": 1}
print(json.dumps(env))
print("%-8s %-14s %-22s %-26s %-26s" % ("lang", "condition", "WER % (95% interval)", "change vs clean (points)", "change vs ffmpeg mulaw"))
for (lang, c), r in table.items():
    print("%-8s %-14s %5.1f (%4.1f to %4.1f)   %+5.1f [%+5.1f, %+5.1f]   %+5.1f [%+5.1f, %+5.1f]" % ((lang, c, r["wer"]) + tuple(r["ci"]) + tuple(r["vs_clean"]) + tuple(r["vs_ffmpeg"])))
with open(OUT + "_clips.csv", "w", newline="") as fh:
    wr = csv.DictWriter(fh, fieldnames=["language", "condition", "clip", "errors", "words"]); wr.writeheader(); wr.writerows(rows)
json.dump({"env": env, "table": {"%s|%s" % k: v for k, v in table.items()}}, open(OUT + "_summary.json", "w"), indent=1)
print("saved", OUT + "_clips.csv", OUT + "_summary.json")
print("Limits: codec and resample only, no real phone network; one run, 30 clips per language, wide intervals; word error only; no comparison with other systems.")
