"""Acoustic measurements of the Parler `description` option, English and Hindi (Colab GPU, gated model). Not a listening test.
Needs: ragleap-voice installed, indic-parler-tts pulled (python -m ragleap_voice models pull indic-parler-tts --allow-unverified, with your own
Hugging Face token), then the token DELETED and the session restarted, and a Python 3.11 worker environment as in issue 990:
uv venv --python 3.11 /content/parler-env, then install parler-tts at the commit named there into it. On the 2026-11-09 Colab run the worker failed
at the tokenizer with "cannot import name runtime_version from google.protobuf" until sentencepiece==0.2.0 was installed into that environment;
that was tried once, on one install. The script refuses to run while a token variable is set (Colab's own COLAB_JUPYTER_TOKEN is ignored).
For each style (neutral, happy, sad, angry, calm) and language (en, hi) it generates STYLE_TAKES takes (default 2) with the engine's description option,
saves the WAVs, and prints duration, RMS level and a median F0 from a plain autocorrelation estimate over voiced 40 ms frames.
The sentences and descriptions are the author's own wording, and the Hindi sentence has not been reviewed by a fluent speaker. No seed is set.
One run, one sentence per language: this separates nothing from the model's sampling randomness and says nothing about whether a clip sounds
like the emotion asked for. That needs listeners (issue 756)."""
import io, json, os, sys, wave, zipfile
import numpy as np
import ragleap_voice
from ragleap_voice import synthesize

bad = [k for k in os.environ if "TOKEN" in k.upper() and k != "COLAB_JUPYTER_TOKEN"]
if bad:
    sys.exit("token variable(s) set: %s. Delete the token and restart before running model code." % bad)
os.environ.setdefault("RAGLEAP_VOICE_PARLER_PYTHON", "/content/parler-env/bin/python")
TAKES = int(os.environ.get("STYLE_TAKES", "2"))
OUT = os.environ.get("STYLE_OUT", "emotion_clips")
BASE = "The recording is of very high quality, with the speaker's voice sounding clear and very close."
STYLES = {
    "neutral": "A female speaker delivers speech in a neutral tone with a moderate speed and pitch. " + BASE,
    "happy": "A female speaker delivers speech in a happy, cheerful tone with a moderate speed and a slightly higher pitch. " + BASE,
    "sad": "A female speaker delivers speech in a sad, subdued tone with a slow speed and a low pitch. " + BASE,
    "angry": "A female speaker delivers speech in an angry, tense tone with a fast speed and a firm pitch. " + BASE,
    "calm": "A female speaker delivers speech in a calm, relaxed tone with a slow speed and a steady pitch. " + BASE,
}
TEXT = {"en": "The meeting has been moved to tomorrow morning.", "hi": "बैठक कल सुबह के लिए टाल दी गई है।"}
os.makedirs(OUT, exist_ok=True)


def wav_bytes(sp):
    for name in ("wav", "audio", "data"):
        v = getattr(sp, name, None)
        if isinstance(v, (bytes, bytearray)):
            return bytes(v)
    sys.exit("no WAV bytes found on the result; attributes: %s" % [a for a in dir(sp) if not a.startswith("_")])


def samples(b):
    with wave.open(io.BytesIO(b), "rb") as w:
        rate, n, ch = w.getframerate(), w.getnframes(), w.getnchannels()
        x = np.frombuffer(w.readframes(n), dtype="<i2").astype(np.float64) / 32768.0
    return (x.reshape(-1, ch).mean(1) if ch > 1 else x), rate


def f0_stats(x, rate):
    fl, hop = int(0.04 * rate), int(0.01 * rate)
    lo, hi = int(rate / 400), int(rate / 70)
    rms_all = np.sqrt(np.mean(x ** 2)) + 1e-12
    f0 = []
    for i in range(0, len(x) - fl, hop):
        fr = x[i:i + fl] - x[i:i + fl].mean()
        if np.sqrt(np.mean(fr ** 2)) < 0.3 * rms_all:
            continue
        ac = np.correlate(fr, fr, "full")[fl - 1:]
        if ac[0] <= 0:
            continue
        k = lo + int(np.argmax(ac[lo:hi]))
        if ac[k] / ac[0] > 0.3:
            f0.append(rate / k)
    if len(f0) < 10:
        return None, None, len(f0)
    return float(np.median(f0)), float(np.subtract(*np.percentile(f0, [75, 25]))), len(f0)


rows = []
for lang, text in TEXT.items():
    for style, desc in STYLES.items():
        for take in range(1, TAKES + 1):
            b = wav_bytes(synthesize(text, language=lang, engine="parler", allow_unverified=True, options={"description": desc}))
            open("%s/%s_%s_%d.wav" % (OUT, lang, style, take), "wb").write(b)
            x, rate = samples(b)
            m, iqr, nv = f0_stats(x, rate)
            rows.append({"lang": lang, "style": style, "take": take, "seconds": round(len(x) / rate, 2), "rate": rate,
                         "rms_dbfs": round(20 * np.log10(np.sqrt(np.mean(x ** 2)) + 1e-12), 1),
                         "f0_median_hz": None if m is None else round(m), "f0_iqr_hz": None if iqr is None else round(iqr), "voiced_frames": nv})
            print(lang, style, take, rows[-1]["seconds"], "s", flush=True)

print("\n%-4s %-8s %-4s %8s %9s %10s %9s %7s" % ("lang", "style", "take", "seconds", "RMS dBFS", "F0 median", "F0 IQR", "voiced"))
for r in rows:
    print("%-4s %-8s %-4d %8.2f %9.1f %10s %9s %7d" % (r["lang"], r["style"], r["take"], r["seconds"], r["rms_dbfs"], r["f0_median_hz"], r["f0_iqr_hz"], r["voiced_frames"]))
json.dump({"ragleap_voice": ragleap_voice.__version__, "styles": STYLES, "text": TEXT, "rows": rows}, open(OUT + "/measurements.json", "w"), ensure_ascii=False, indent=1)
with zipfile.ZipFile(OUT + ".zip", "w") as z:
    for fn in sorted(os.listdir(OUT)):
        z.write(os.path.join(OUT, fn))
print("saved", OUT + ".zip")
print("Limits: one sentence per language, %d take(s) per style, one run; duration, level and a rough autocorrelation pitch only; not emotion quality." % TAKES)
