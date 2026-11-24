"""Whisper large-v3 sweep over many FLEURS languages (60 clips each), in batches (docs/design/coverage-matrix.md section 6).

Run in Google Colab (T4 GPU runtime), pasted into one cell, after setting the environment in an earlier cell, for example:
    import os; os.environ["S_BATCH"] = "A"
S_BATCH  A, B, C, D or E (default A), or R (re-measure ja and en with this script); S_ONLY="de,it" runs only those codes from the batch table instead.
S_OUT    results folder (default /content/drive/MyDrive/whisper_sweep; Drive is mounted if needed). A language whose result files
         are already there is skipped, so a recycled session can be resumed.
S_N      clips per language (default 60).  S_FW  faster-whisper version (default 1.2.1, the one the whisper extra pins; the pilot,
         colab_pilot12.py, used 1.1.1; set S_FW=1.1.1 to reproduce its setup).
No token needed: FLEURS and the model are not gated. Same method as colab_pilot12.py: the model is pulled with 'ragleap-voice models
pull' (pinned revision, hash-checked), float16, beam 5, temperature 0, language given, FLEURS test split, first N clips. Licence gates
stop the run if FLEURS is not CC-BY-4.0 or the model is not MIT.
Limits: read speech only; consecutive clips, so intervals are probably too narrow; one system; one run; WER is not meaningful for
languages without word spaces (ja zh yue th my km lo), and the CER this script prints for them counts the spaces that FLEURS puts
between Chinese characters (Mandarin, batch A: 48.4% printed, 7.5% after removing spaces and punctuation): run
colab_whisper_sweep_analysis.py for the figures to quote. A FLEURS config name that does not exist is skipped, not guessed.
This script was written without a run; the first run is its test.
"""
import csv, ctypes, glob, importlib.metadata as md, os, pathlib, site, subprocess, sys, time, wave

def sh(*a): subprocess.run(list(a), check=True)
def out(*a):
    try: return subprocess.run(list(a), capture_output=True, text=True).stdout.strip()
    except FileNotFoundError: return ""

FW = os.environ.get("S_FW", "1.2.1")
N = int(os.environ.get("S_N", "60"))
BATCH = os.environ.get("S_BATCH", "A").upper()
ONLY = [c.strip() for c in os.environ.get("S_ONLY", "").split(",") if c.strip()]
CHAR = {"ja", "zh", "yue", "th", "my", "km", "lo"}  # no word spaces: read CER

BATCHES = {
    "A": {"de": "de_de", "it": "it_it", "ko": "ko_kr", "zh": "cmn_hans_cn", "pl": "pl_pl", "nl": "nl_nl", "uk": "uk_ua",
          "he": "he_il", "th": "th_th", "fa": "fa_ir", "ro": "ro_ro", "cs": "cs_cz"},
    "B": {"el": "el_gr", "hu": "hu_hu", "fi": "fi_fi", "da": "da_dk", "sv": "sv_se", "no": "nb_no", "bg": "bg_bg",
          "hr": "hr_hr", "sk": "sk_sk", "sl": "sl_si", "sr": "sr_rs", "ca": "ca_es"},
    "C": {"bn": "bn_in", "mr": "mr_in", "gu": "gu_in", "pa": "pa_in", "ur": "ur_pk", "ne": "ne_np", "ms": "ms_my",
          "tl": "fil_ph", "km": "km_kh", "lo": "lo_la", "my": "my_mm", "as": "as_in"},
    "D": {"af": "af_za", "ha": "ha_ng", "ln": "ln_cd", "sn": "sn_zw", "so": "so_so", "lt": "lt_lt", "lv": "lv_lv",
          "et": "et_ee", "mt": "mt_mt", "is": "is_is", "cy": "cy_gb", "gl": "gl_es"},
    "E": {"az": "az_az", "ka": "ka_ge", "hy": "hy_am", "kk": "kk_kz", "uz": "uz_uz", "tg": "tg_tj", "be": "be_by",
          "mk": "mk_mk", "mn": "mn_mn", "ps": "ps_af", "sd": "sd_in", "bs": "bs_ba", "lb": "lb_lu", "oc": "oc_fr",
          "yue": "yue_hant_hk"},
    "R": {"ja": "ja_jp", "en": "en_us"},  # re-measure with this script (the pilot had 20 clips for en)
}
if ONLY:
    allcodes = {c: cfg for b in BATCHES.values() for c, cfg in b.items()}
    for c in ONLY:
        if c not in allcodes: print("unknown code in S_ONLY:", c)
    SEL = {c: allcodes[c] for c in ONLY if c in allcodes}
else:
    assert BATCH in BATCHES, "S_BATCH must be one of %s" % ", ".join(BATCHES)
    SEL = BATCHES[BATCH]
print("languages selected:", SEL)

sh(sys.executable, "-m", "pip", "install", "-q", "faster-whisper==" + FW, "datasets==3.6.0", "soundfile",
   "nvidia-cublas-cu12", "nvidia-cudnn-cu12", "--force-reinstall", "--no-deps", "--no-cache-dir",
   "git+https://github.com/ragleap/ragleap-core.git#subdirectory=packages/ragleap-voice")
sh(sys.executable, "-m", "pip", "install", "-q", "faster-whisper==" + FW, "datasets==3.6.0", "soundfile",
   "nvidia-cublas-cu12", "nvidia-cudnn-cu12")
gpu = out("nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader")
assert gpu, "No GPU: Runtime > Change runtime type > T4 GPU, press Save"
main_sha = out("git", "ls-remote", "https://github.com/ragleap/ragleap-core.git", "refs/heads/main").split()[0]

libs = []
for r in site.getsitepackages() + [site.getusersitepackages()]:
    libs += sorted(glob.glob(r + "/nvidia/cublas/lib/libcublas*.so.12"), reverse=True)
    libs += sorted(glob.glob(r + "/nvidia/cudnn/lib/libcudnn*.so.9"))
loaded = []
for p in libs:
    try: ctypes.CDLL(p, mode=ctypes.RTLD_GLOBAL); loaded.append(p.split("/")[-1])
    except OSError as e: print("could not load", p.split("/")[-1], str(e)[:80])
assert any(x.startswith("libcublas.so") for x in loaded), "cuBLAS not loaded (environment problem)"

from huggingface_hub import HfApi
api = HfApi()
def lic(info):
    v = info.card_data.license
    return [x.lower() for x in ([v] if isinstance(v, str) else (v or []))]
assert lic(api.dataset_info("google/fleurs")) == ["cc-by-4.0"], "FLEURS licence is not CC-BY-4.0: STOP"
assert lic(api.model_info("Systran/faster-whisper-large-v3")) == ["mit"], "Whisper licence is not MIT: STOP"

OUT = pathlib.Path(os.environ.get("S_OUT", "/content/drive/MyDrive/whisper_sweep"))
if str(OUT).startswith("/content/drive") and not os.path.isdir("/content/drive/MyDrive"):
    try:
        from google.colab import drive; drive.mount("/content/drive")
    except Exception as e:
        print("Drive not mounted (%s): results go to /content/whisper_sweep and are LOST when the session is recycled" % str(e)[:80])
        OUT = pathlib.Path("/content/whisper_sweep")
OUT.mkdir(parents=True, exist_ok=True)

RV = "/content/rv_cache"
r = subprocess.run([sys.executable, "-m", "ragleap_voice", "models", "pull", "whisper-large-v3",
                    "--cache-dir", RV, "--allow-unverified"], capture_output=True, text=True)
print(r.stdout[-400:]); print(r.stderr[-400:])
assert r.returncode == 0, "pull failed: stop here and send this output"
from ragleap_voice.manifest import load_manifest
W = {e.name: e for e in load_manifest()[0]}["whisper-large-v3"]
MODEL_DIR = "%s/models/whisper-large-v3/%s" % (RV, W.revision)
assert pathlib.Path(MODEL_DIR, "model.bin").is_file(), MODEL_DIR

import numpy as np
from datasets import load_dataset, get_dataset_config_names
from faster_whisper import WhisperModel
from ragleap_voice.bench import load_manifest as load_items, run_stt, summarize, environment_info, write_results
model = WhisperModel(MODEL_DIR, device="cuda", compute_type="float16")
supported = set(getattr(model, "supported_languages", []) or [])
configs = set(get_dataset_config_names("google/fleurs", trust_remote_code=True))

def engine(path, lang):
    with wave.open(path, "rb") as wf:
        audio = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2").astype("float32") / 32768.0
    segs, _ = model.transcribe(audio, language=lang, beam_size=5, temperature=0.0, condition_on_previous_text=False)
    return " ".join(s.text.strip() for s in segs)

warm, finished, skipped, failed = False, [], [], []
for code, cfg in SEL.items():
    tag = "whisper-large-v3-sweep-" + code
    if glob.glob(str(OUT / ("*" + tag + "*"))): print("DONE EARLIER", code); finished.append(code); continue
    if cfg not in configs: print("SKIP", code, ": no FLEURS config", cfg); skipped.append(code); continue
    if supported and code not in supported: print("SKIP", code, ": not a Whisper language code"); skipped.append(code); continue
    try:
        t0 = time.time()
        root = pathlib.Path("/content/fleurs_sweep") / code; root.mkdir(parents=True, exist_ok=True)
        rows = []
        ds = load_dataset("google/fleurs", cfg, split="test", streaming=True, trust_remote_code=True)
        for i, ex in enumerate(ds):
            if i >= N: break
            a = ex["audio"]; assert a["sampling_rate"] == 16000
            pcm = (np.clip(a["array"], -1, 1) * 32767).astype("<i2")
            name = "%s_%02d.wav" % (code, i)
            with wave.open(str(root / name), "wb") as wf:
                wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(16000); wf.writeframes(pcm.tobytes())
            rows.append(["%s_%02d" % (code, i), name, ex["transcription"], code, "CC-BY-4.0", "google/fleurs test, config " + cfg])
        with open(root / "manifest.csv", "w", encoding="utf-8", newline="") as f:
            cw = csv.writer(f); cw.writerow(["id", "path", "reference", "language", "license", "source"]); cw.writerows(rows)
        items, problems = load_items(root / "manifest.csv")
        assert not problems, problems
        if not warm: engine(items[0].path, items[0].language); warm = True  # warm-up, not timed
        res = run_stt(engine, items)
        meta = environment_info(gpu=gpu, engine="faster-whisper " + md.version("faster-whisper"),
            model="Systran/faster-whisper-large-v3", model_revision=W.revision, compute_type="float16", beam_size=5, temperature=0.0,
            model_source="ragleap-voice models pull (hash-checked, tagged lock)",
            dataset="google/fleurs test, first %d, config %s" % (N, cfg), harness_commit_main=main_sha,
            datasets_version=md.version("datasets"), languages=code)
        csvp, jsonp = write_results(str(OUT), tag, res, meta)
        print("%s (%s)%s, %d clips, %.0f s:" % (code, cfg, " [no word spaces: the CER printed here counts the reference spaces; use the analysis script]" if code in CHAR else "", len(items), time.time() - t0))
        for k, v in summarize(res).items():
            print("  ", k, {a: (round(b, 4) if isinstance(b, float) else b) for a, b in v.items()})
        finished.append(code)
    except Exception as e:
        print("FAILED", code, type(e).__name__, str(e)[:200]); failed.append(code)
print("finished:", finished, "| skipped:", skipped, "| failed:", failed)
print("GPU:", gpu, "| faster-whisper", md.version("faster-whisper"), "| model rev:", W.revision[:10], "| harness main:", main_sha[:10])
print("results in", OUT)
