"""How many utterances SileroGate reports per clip for several hangover_windows values (Colab, CPU, no token). Not a correctness test.
Needs: pip install "ragleap-voice[vad]>=0.3.6" datasets==3.6.0 numpy. The pinned Silero file is fetched from the raw GitHub URL below and SileroGate
refuses it if the SHA-256 differs. Data: the first HANGOVER_CLIPS (default 30) google/fleurs test clips (CC-BY-4.0) for English (en_us) and Hindi (hi_in).
One window is 32 ms. For each hangover value it prints the mean and median number of utterances per clip, how many clips gave exactly one, and the
coverage: the share of each clip's duration between a speech_start and its speech_end. A segment ends at the last speech window, so the hangover is not
counted, but a segment still open when the stream ends is closed at the stream end, so at long hangovers trailing silence is counted and coverage is
not a measure of speech kept. The script counts utterances only: it assumes one read sentence should be one utterance and does not check whether a
merged pause was a real break. Clean read speech, no noise, one run, default probability 0.5 and three windows. It recommends no default."""
import array, os, statistics, urllib.request
import numpy as np
from datasets import load_dataset
import ragleap_voice
from ragleap_voice.vad_silero import SileroGate, WINDOW_MS

URL = "https://raw.githubusercontent.com/snakers4/silero-vad/066409f3d9db45a30d21dfcbf9147826ffdd7052/src/silero_vad/data/silero_vad_16k_op15.onnx"
urllib.request.urlretrieve(URL, "silero_vad_16k_op15.onnx")
N = int(os.environ.get("HANGOVER_CLIPS", "30"))
HANG = (6, 12, 25, 50, 100)


def to_arr(x):
    a = array.array("h"); a.frombytes(np.clip(np.round(x), -32768, 32767).astype("<i2").tobytes()); return a


clips = {}
for lang, cfg in (("en", "en_us"), ("hi", "hi_in")):
    clips[lang] = []
    for ex in load_dataset("google/fleurs", cfg, split="test", streaming=True, trust_remote_code=True):
        clips[lang].append(to_arr(np.asarray(ex["audio"]["array"], dtype=np.float64) * 32767))
        if len(clips[lang]) == N:
            break


def segments(samples, hang):
    g = SileroGate(16000, "silero_vad_16k_op15.onnx", {"hangover_windows": hang})
    ev = []
    for i in range(0, len(samples), 3200):
        ev += g.feed(samples[i:i + 3200])
    ev += g.finish()
    segs, start = [], None
    for e in ev:
        if e.kind == "speech_start":
            start = e.frame
        elif e.kind == "speech_end" and start is not None:
            segs.append((start, e.frame)); start = None
    return segs


print("ragleap-voice", ragleap_voice.__version__, "| window ms:", WINDOW_MS)
print("%-4s %9s %8s %10s %10s %14s %10s" % ("lang", "hangover", "seconds", "mean utt", "median", "clips with 1", "coverage"))
for lang in clips:
    for h in HANG:
        counts, cover = [], []
        for c in clips[lang]:
            s = segments(c, h)
            counts.append(len(s))
            cover.append(sum(b - a for a, b in s) * WINDOW_MS / 1000.0 / (len(c) / 16000))
        print("%-4s %9d %8.2f %10.2f %10.1f %9d of %d %9.0f%%" % (lang, h, h * WINDOW_MS / 1000, statistics.mean(counts), statistics.median(counts),
              sum(1 for k in counts if k == 1), len(counts), 100 * statistics.mean(cover)))
print("Limits: %d clean read-speech clips per language, one run; counts utterances, not correct splits; coverage includes trailing silence at long hangovers; no recommended default." % N)
