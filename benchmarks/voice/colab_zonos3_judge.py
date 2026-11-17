"""Round-trip check of the ZONOS3-Mini smoke clips (colab_zonos3_smoke.py): each clip is read back by IndicConformer-600m (the same
judge and the same scoring as colab_tts_roundtrip.py) and compared with the FLEURS sentence it was made from.
NOT RUN YET when this file was written. It compiles; its first run is its test.

How to run: first the smoke script, with Z_N=5 (five sentences per language), Z_OUT and Z_CACHE on Google Drive, and NO token in the
notebook secrets. Then this script in a second cell, on a GPU runtime with Drive mounted, after the server has stopped.
It needs a Hugging Face token: IndicConformer is a gated model whose conditions the account must have accepted. The token is read from
Colab Secrets (HF_TOKEN, Notebook access on) in the first seconds, so keep the tab in the foreground. The model's own code runs with
trust_remote_code=True while the token exists: use a fresh short-lived Read token and delete the secret afterwards.
The first sentences are the same as the first sentences of colab_tts_roundtrip.py (same filter, same order), so Zonos clips can be
compared with Parler clips of the same sentences. Read the limits printed at the end."""
import subprocess, sys, os, re, json, pathlib, glob, ctypes, site

HARNESS_PIN = "e02b291c6972b11794cdc1d0f60475c5156e8000"   # ragleap-core main on 2026-11-17
IC = "ai4bharat/indic-conformer-600m-multilingual"
OUT = pathlib.Path(os.environ.get("Z_OUT", "/content/drive/MyDrive/zonos3_smoke"))
RES = pathlib.Path(os.environ.get("Z_JUDGE_OUT", str(OUT / "judge")))
FLAG_CER = 0.5     # a clip with more than this share of characters wrong is listed
FLAG_GAP = 0.3     # the two voices differ by more than this on the same sentence: listed


def say(*a):
    print(*a, flush=True)


def stop(msg):
    say("\nSTOP: " + msg)
    sys.exit(2)


def run(*a, **k):
    return subprocess.run(list(a), capture_output=True, text=True, **k)


def pip(*pk):
    r = run(sys.executable, "-m", "pip", "install", "-q", *pk)
    if r.returncode:
        stop("pip install failed (environment problem): " + r.stderr[-600:])


def find_rows(o):
    if isinstance(o, list) and o and isinstance(o[0], dict) and "id" in o[0] and "text" in o[0]:
        return o
    if isinstance(o, dict):
        for v in o.values():
            r = find_rows(v)
            if r:
                return r
    return None


jf = OUT / "zonos3_smoke.json"
if not jf.exists():
    stop("no %s: run colab_zonos3_smoke.py first, with Z_OUT on Drive, or set Z_OUT to its folder." % jf)
data = json.load(open(jf, encoding="utf-8"))
clips = find_rows(data)
if not clips:
    stop("no clip list with id and text in %s; top-level keys: %s" % (jf, list(data)[:12]))
clips = [c for c in clips if not c.get("error") and (OUT / (c["id"] + ".wav")).exists()]
if not clips:
    stop("no usable clips (no WAV file next to the results file, or every clip had an error).")
say("clips to judge: %d, from %s" % (len(clips), OUT))

from google.colab import userdata
try:
    tok = userdata.get("HF_TOKEN")
except Exception:
    stop("secret fetch failed: keep this tab in the foreground, check the key icon (HF_TOKEN present, Notebook access on), run again.")
gpu = run("nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits").stdout.strip()
if not gpu:
    stop("no GPU: Runtime > Change runtime type > T4 GPU.")

pip("soundfile", "huggingface_hub")
pip("nvidia-cublas-cu12", "nvidia-cudnn-cu12",
    "git+https://github.com/ragleap/ragleap-core.git@%s#subdirectory=packages/ragleap-voice" % HARNESS_PIN)
pip("transformers==5.18.0", "onnxruntime-gpu==1.31.0", "onnx==1.20.1", "torchaudio")
libs = []
for r in site.getsitepackages() + [site.getusersitepackages()]:
    libs += sorted(glob.glob(r + "/nvidia/cublas/lib/libcublas*.so.12"), reverse=True)
    libs += sorted(glob.glob(r + "/nvidia/cudnn/lib/libcudnn*.so.9"))
loaded = []
for p in libs:
    try:
        ctypes.CDLL(p, mode=ctypes.RTLD_GLOBAL)
        loaded.append(p.split("/")[-1])
    except OSError:
        pass
if not any(x.startswith("libcublas.so") for x in loaded):
    stop("cuBLAS not loaded (environment problem).")

import torch, torchaudio, soundfile as sf, huggingface_hub
from huggingface_hub import HfApi, snapshot_download

api = HfApi()
mi = api.model_info(IC, token=tok)
lic = mi.card_data.license if mi.card_data else None
if [x.lower() for x in ([lic] if isinstance(lic, str) else (lic or []))] != ["mit"]:
    stop("IndicConformer licence changed or not readable: %s" % (lic,))
local = snapshot_download(IC, revision=mi.sha, token=tok, local_dir="/content/ic_model")
from transformers import AutoModel
for k in [k for k in sys.modules if "model_onnx" in k]:
    del sys.modules[k]
_orig = huggingface_hub.snapshot_download
huggingface_hub.snapshot_download = lambda repo_id, *a, **k: local if repo_id == IC else _orig(repo_id, *a, **k)
try:
    ic = AutoModel.from_pretrained(IC, revision=mi.sha, trust_remote_code=True, token=tok)
finally:
    huggingface_hub.snapshot_download = _orig


def to16k(path):
    x, sr = sf.read(path, dtype="float32", always_2d=True)
    x = torch.from_numpy(x.mean(axis=1))
    if sr != 16000:
        x = torchaudio.functional.resample(x, sr, 16000)
    return x.numpy().astype("float32")


def judge(path, lang):
    out = ic(torch.from_numpy(to16k(path)).unsqueeze(0), lang, "rnnt")
    return str(out[0] if isinstance(out, (list, tuple)) else out).strip()


from ragleap_voice.bench import TestItem, run_stt, environment_info, write_results
meta_of = {c["id"]: c for c in clips}
items = [TestItem(c["id"], str(OUT / (c["id"] + ".wav")), c["text"], c["lang"], "CC-BY-4.0",
                  "FLEURS sentence text, synthesized by ZONOS3-Mini") for c in clips]
rows = run_stt(judge, items)
RES.mkdir(parents=True, exist_ok=True)
write_results(str(RES), "zonos3-roundtrip", rows,
              environment_info(gpu=gpu, judge="indic-conformer-600m RNNT", ic_revision=mi.sha, harness_pin=HARNESS_PIN,
                               smoke_results=str(jf)))


def rate(e, n):
    return e / n if n else None


say("\nper clip: audio seconds, reference characters per second, judge output length / reference length, word and character error")
cer_of, pooled = {}, {}
for r in rows:
    c = meta_of[r["id"]]
    k = r["id"].rsplit("_", 1)[1]
    if r["error"]:
        say("  %-16s judge error: %s" % (r["id"], r["error"][:120]))
        continue
    cer, wer = rate(r["char_errors"], r["ref_chars"]), rate(r["word_errors"], r["ref_words"])
    cps = len(r["reference"]) / r["audio_s"] if r["audio_s"] else 0
    ratio = len(r["hypothesis"]) / max(1, len(r["reference"]))
    say("  %-16s %5.1f s  %5.1f c/s  length ratio %4.2f  WER %5.1f%%  CER %5.1f%%" % (
        r["id"], r["audio_s"], cps, ratio, 100 * (wer or 0), 100 * (cer or 0)))
    cer_of[(c["lang"], k, c["voice"])] = cer
    p = pooled.setdefault((c["lang"], c["voice"]), [0, 0, 0, 0])
    p[0] += r["char_errors"]; p[1] += r["ref_chars"]; p[2] += r["word_errors"]; p[3] += r["ref_words"]

say("\npooled per language and voice (few sentences: no confidence interval, read as a hint only)")
for (lang, voice), p in sorted(pooled.items()):
    say("  %s %-7s CER %5.1f%%  WER %5.1f%%" % (lang, voice, 100 * p[0] / max(1, p[1]), 100 * p[2] / max(1, p[3])))

say("\nclips with more than %d%% of characters wrong:" % int(100 * FLAG_CER))
bad = sorted("%s_%s_%s" % (l, v, k) for (l, k, v), x in cer_of.items() if x is not None and x > FLAG_CER)
say("  " + (", ".join(bad) if bad else "none"))
say("same sentence, the two voices differ by more than %d points of CER:" % int(100 * FLAG_GAP))
gaps = []
for (l, k, v), x in cer_of.items():
    o = [y for (l2, k2, v2), y in cer_of.items() if l2 == l and k2 == k and v2 != v]
    if o and x is not None and o[0] is not None and x - o[0] > FLAG_GAP:
        gaps.append("%s_%s_%s (%.0f%% against %.0f%%)" % (l, v, k, 100 * x, 100 * o[0]))
say("  " + (", ".join(sorted(gaps)) if gaps else "none"))
say("\nLimits: one run, seed 1, few sentences, two stock voices. The judge is itself a model with its own errors on these languages, and "
    "it was not calibrated on this audio, so a high error can mean wrong speech or a weak judge. A low length ratio with high error "
    "points to cut or dropped speech; a normal ratio with high error points to wrong sounds or a weak judge. Nothing here says the "
    "speech is natural, in the right accent or expressive. Tamil, Malayalam and Kannada were spoken by a Hindi or Telugu voice. "
    "Do not commit the WAV files. Delete the HF_TOKEN secret now.")
