"""Round trip of the ZONOS3-Mini clips made with Z_WORLD=1 (colab_zonos3_smoke.py): each clip is read back by Whisper large-v3 and compared with
the FLEURS sentence it was made from. A CONTROL is read the same way: the real FLEURS recording of the same sentence, so the judge's own error
on human speech is printed next to its error on synthetic speech.

How to run (Colab, T4 GPU runtime, Drive mounted, no token: FLEURS and Whisper are not gated): first the smoke script with the environment
Z_WORLD=1, Z_N=10, Z_OUT=/content/drive/MyDrive/zonos3_world and Z_CACHE=<a Drive folder>; then this script (%run) with the same Z_OUT.
Whisper is pulled with 'ragleap-voice models pull' (pinned revision, hash-checked), float16, beam 5, temperature 0, language given.
Word error rate for spaced languages (the harness normalizer version 1); for Mandarin the character error rate after Unicode NFKC, lower case
and removing spaces and punctuation from both sides. With 10 sentences per language no interval is printed (it would mean nothing).
Limits: one run, seed 1, few sentences, one stock voice per language (French has only a male one), intelligibility only: nothing here says
whether the speech is natural, in the right accent or expressive, and nobody listened. The judge has its own errors, which the control shows.
Written without a run; the first run is its test."""
import csv, ctypes, glob, importlib.metadata as md, json, os, pathlib, re, site, subprocess, sys, unicodedata, wave
from math import gcd

OUT = pathlib.Path(os.environ.get("Z_OUT", "/content/drive/MyDrive/zonos3_world"))
RES = pathlib.Path(os.environ.get("Z_JUDGE_OUT", str(OUT / "judge_whisper")))
WHISPER = {"en_us": "en", "fr_fr": "fr", "de": "de", "es": "es", "it": "it", "pt_br": "pt", "cmn": "zh", "ko": "ko"}
FLEURS = {"en_us": "en_us", "fr_fr": "fr_fr", "de": "de_de", "es": "es_419", "it": "it_it", "pt_br": "pt_br", "cmn": "cmn_hans_cn", "ko": "ko_kr"}
CHAR = {"cmn"}      # no word spaces: character error rate
SCAN = 1500         # FLEURS examples scanned per language for the control recordings
FLAG_RATIO = 0.85   # judge output shorter than this share of the reference: listed


def say(*a): print(*a, flush=True)
def stop(msg, code=2): say("\nSTOP: " + msg); sys.exit(code)
def sh(*a): subprocess.run(list(a), check=True)
def out(*a):
    try: return subprocess.run(list(a), capture_output=True, text=True).stdout.strip()
    except FileNotFoundError: return ""


def squash(s):
    s = unicodedata.normalize("NFKC", s or "").lower()
    return "".join(ch for ch in s if not ch.isspace() and unicodedata.category(ch)[0] not in "PZ")


def ed(a, b):
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def rate(rows, char):
    """Error rate in percent from result rows (columns of the harness CSV)."""
    if char:
        e = n = 0
        for r in rows:
            a, b = squash(r["reference"]), squash(r["hypothesis"])
            e += ed(a, b); n += len(a)
        return 100.0 * e / max(1, n)
    return 100.0 * sum(int(r["word_errors"]) for r in rows) / max(1, sum(int(r["ref_words"]) for r in rows))


def pick_control(examples, needed, limit=SCAN):
    """examples: iterable of (raw text, audio, rate); needed: {sentence: id}. Returns {id: (audio, rate)}, the first recording of each."""
    found, seen = {}, 0
    for text, audio, sr in examples:
        seen += 1
        t = (text or "").strip()
        if t in needed and needed[t] not in found:
            found[needed[t]] = (audio, sr)
        if len(found) == len(needed) or seen >= limit:
            break
    return found


def read_wav(path):
    import numpy as np
    with wave.open(str(path), "rb") as w:
        assert w.getsampwidth() == 2, "16-bit PCM expected"
        n, sr, ch = w.getnframes(), w.getframerate(), w.getnchannels()
        x = np.frombuffer(w.readframes(n), dtype="<i2").astype("float32") / 32768.0
    return (x.reshape(-1, ch).mean(axis=1) if ch > 1 else x), sr


def to16k(x, sr):
    import numpy as np
    x = np.asarray(x, dtype="float32")
    if x.ndim > 1: x = x.mean(axis=1)
    if int(sr) != 16000:
        from scipy.signal import resample_poly
        g = gcd(16000, int(sr)); x = resample_poly(x, 16000 // g, int(sr) // g).astype("float32")
    return x


def write16k(path, x):
    import numpy as np
    pcm = (np.clip(x, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(pcm.tobytes())


def main():
    smoke = OUT / "zonos3_smoke.json"
    if not smoke.exists(): stop("no %s: run the smoke script with Z_WORLD=1 and the same Z_OUT first." % smoke)
    data = json.loads(smoke.read_text(encoding="utf-8"))
    rows = [r for r in data["rows"] if not r["error"] and r["lang"] in WHISPER and (OUT / (r["id"] + ".wav")).exists()]
    if not rows: stop("no usable world clips in %s (was the smoke run made with Z_WORLD=1?)." % smoke)
    say("clips to judge: %d in %d languages" % (len(rows), len({r["lang"] for r in rows})))
    FW = os.environ.get("S_FW", "1.2.1")
    sh(sys.executable, "-m", "pip", "install", "-q", "faster-whisper==" + FW, "datasets==3.6.0", "soundfile", "scipy",
       "nvidia-cublas-cu12", "nvidia-cudnn-cu12", "--force-reinstall", "--no-deps", "--no-cache-dir",
       "git+https://github.com/ragleap/ragleap-core.git#subdirectory=packages/ragleap-voice")
    sh(sys.executable, "-m", "pip", "install", "-q", "faster-whisper==" + FW, "datasets==3.6.0", "soundfile", "scipy",
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
        except OSError as e: say("could not load", p.split("/")[-1], str(e)[:80])
    assert any(x.startswith("libcublas.so") for x in loaded), "cuBLAS not loaded (environment problem)"
    from huggingface_hub import HfApi
    api = HfApi()
    def lic(info):
        v = info.card_data.license
        return [x.lower() for x in ([v] if isinstance(v, str) else (v or []))]
    assert lic(api.dataset_info("google/fleurs")) == ["cc-by-4.0"], "FLEURS licence is not CC-BY-4.0: STOP"
    assert lic(api.model_info("Systran/faster-whisper-large-v3")) == ["mit"], "Whisper licence is not MIT: STOP"
    RV = "/content/rv_cache"
    r = subprocess.run([sys.executable, "-m", "ragleap_voice", "models", "pull", "whisper-large-v3", "--cache-dir", RV, "--allow-unverified"],
                       capture_output=True, text=True)
    say(r.stdout[-300:]); say(r.stderr[-300:])
    assert r.returncode == 0, "pull failed: stop here and send this output"
    from ragleap_voice.manifest import load_manifest
    W = {e.name: e for e in load_manifest()[0]}["whisper-large-v3"]
    MODEL_DIR = "%s/models/whisper-large-v3/%s" % (RV, W.revision)
    assert pathlib.Path(MODEL_DIR, "model.bin").is_file(), MODEL_DIR
    import numpy as np
    from datasets import load_dataset
    from faster_whisper import WhisperModel
    from ragleap_voice.bench import TestItem, run_stt, environment_info, write_results
    model = WhisperModel(MODEL_DIR, device="cuda", compute_type="float16")

    def engine(path, lang):
        x, _ = read_wav(path)
        segs, _i = model.transcribe(x, language=lang, beam_size=5, temperature=0.0, condition_on_previous_text=False)
        return " ".join(s.text.strip() for s in segs)

    work = pathlib.Path("/content/zw"); (work / "synth").mkdir(parents=True, exist_ok=True); (work / "control").mkdir(exist_ok=True)
    synth, needed, lang_of, voice_of = [], {}, {}, {}
    for r in rows:
        x, sr = read_wav(OUT / (r["id"] + ".wav"))
        p = work / "synth" / (r["id"] + ".wav"); write16k(p, to16k(x, sr))
        synth.append(TestItem(r["id"], str(p), r["text"], WHISPER[r["lang"]], "CC-BY-4.0", "FLEURS sentence text, synthesized by ZONOS3-Mini"))
        needed.setdefault(r["lang"], {})[r["text"].strip()] = r["id"]
        lang_of[r["id"]] = r["lang"]; voice_of[r["lang"]] = r["voice"]
    control = []
    for lang, need in needed.items():
        ds = load_dataset("google/fleurs", FLEURS[lang], split="test", streaming=True, trust_remote_code=True)
        def gen():
            for ex in ds:
                a = ex["audio"]
                yield ex["raw_transcription"], a["array"], a["sampling_rate"]
        found = pick_control(gen(), need)
        text_of = {v: k for k, v in need.items()}
        for tid, (arr, sr) in found.items():
            p = work / "control" / (tid + ".wav"); write16k(p, to16k(arr, sr))
            control.append(TestItem(tid, str(p), text_of[tid], WHISPER[lang], "CC-BY-4.0", "FLEURS real recording of the same sentence"))
        say("  %-6s control recordings found for %d of %d sentences" % (lang, len(found), len(need)))
    meta = environment_info(gpu=gpu, engine="faster-whisper " + md.version("faster-whisper"), model="Systran/faster-whisper-large-v3",
                            model_revision=W.revision, compute_type="float16", beam_size=5, temperature=0.0, judge="whisper-large-v3",
                            harness_commit_main=main_sha, smoke_results=str(smoke))
    engine(synth[0].path, synth[0].language)  # warm-up, not timed
    RES.mkdir(parents=True, exist_ok=True)
    cs, _ = write_results(str(RES), "zonos3-world-synth", run_stt(engine, synth), meta)
    cc, _ = write_results(str(RES), "zonos3-world-control", run_stt(engine, control), meta)
    S = list(csv.DictReader(open(cs, encoding="utf-8")))
    C = {r["id"]: r for r in csv.DictReader(open(cc, encoding="utf-8"))}
    lines = ["", "%-6s %-9s %3s  %-12s %-16s %-14s %s" % ("lang", "voice", "n", "synthetic", "same sentences", "control", "metric"),
             "%-6s %-9s %3s  %-12s %-16s %-14s %s" % ("", "", "", "(all clips)", "(with a control)", "(real speech)", "")]
    flagged = []
    for lang in WHISPER:
        sr_ = [r for r in S if lang_of.get(r["id"]) == lang]
        if not sr_: continue
        paired = [r for r in sr_ if r["id"] in C]
        char = lang in CHAR
        lines.append("%-6s %-9s %3d  %5.1f%%       %-16s %-14s %s" % (lang, voice_of[lang], len(sr_), rate(sr_, char),
                     ("%5.1f%% (n=%d)" % (rate(paired, char), len(paired))) if paired else "no control",
                     ("%5.1f%%" % rate([C[r["id"]] for r in paired], char)) if paired else "-", "CER*" if char else "WER"))
        for r in sr_:
            a, b = squash(r["reference"]), squash(r["hypothesis"])
            if a and len(b) < FLAG_RATIO * len(a): flagged.append((r["id"], len(a), len(b)))
    failed = [r["id"] for r in S if (r.get("error") or "").strip()]
    lines += ["", "* CER after NFKC, lower case, and removing spaces and punctuation from both sides.",
              "judge output shorter than %.0f%% of the reference (cut or dropped speech, or a weak judge): %s" % (100 * FLAG_RATIO, flagged or "none"),
              "judge failures: %s" % (failed or "none")]
    rtf = {}
    for r in rows:
        if r.get("rtf"): rtf.setdefault(r["lang"], []).append(r["rtf"])
    lines.append("generation real-time factor, median per language: " + ", ".join(
        "%s %.2f" % (k, sorted(v)[len(v) // 2]) for k, v in rtf.items()))
    lines += ["", "worst clip per language (reference | judge output):"]
    for lang in WHISPER:
        cand = [r for r in S if lang_of.get(r["id"]) == lang]
        if cand:
            w = max(cand, key=lambda r: ed(squash(r["reference"]), squash(r["hypothesis"])) / max(1, len(squash(r["reference"]))))
            lines.append("  %s %s | %s" % (w["id"], w["reference"][:70], w["hypothesis"][:70]))
    lines += ["", "Limits: one run, seed 1, few sentences, one stock voice per language, intelligibility only, nobody listened, the judge has its own",
              "errors (see the control column), no interval because the samples are small."]
    text = "\n".join(lines)
    say(text)
    (RES / "summary.txt").write_text(text, encoding="utf-8")
    say("\nresults in", RES, "| harness main:", main_sha[:10], "| model rev:", W.revision[:10], "|", gpu)


if __name__ == "__main__":
    main()
