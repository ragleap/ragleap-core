"""Smoke test of ZONOS3-Mini on a Colab GPU: build the ggml server, run it on 127.0.0.1 only, make a few sentences per language.
Run once so far, on one T4: all 20 clips came out, but two were much shorter than their text suggests, which is why the summary now prints characters per second. It measures nothing about quality, naturalness, emotion or accent: it only shows whether the
build works, the server answers, the five language codes (hi, ta, te, ml, kn) are accepted and audio comes out. A failed build or an
environment problem is printed as such and is never a result about the model.
Needs a Colab GPU runtime and no Hugging Face token (the weights are public). Takes over an hour on a T4 (the build alone took 3635 s in the first run). Mount Google Drive first or set Z_OUT, because results kept in /content are lost when Colab recycles the runtime. Set Z_CACHE to a folder on Drive to keep the built server between runs (opt-in; the key is the code pin, the GPU architecture and the nvcc version; not tested yet).
Prompts are FLEURS test sentence TEXT (CC-BY-4.0, no audio), the same filter as colab_tts_roundtrip.py.
Stock voices: Ananya (Hindi, female) and Sravya (Telugu, female). Tamil, Malayalam and Kannada have no stock voice, so those sentences are
spoken by a Hindi or Telugu voice: that can test intelligibility only, never accent. No real person's voice is cloned.
Pins: code Zyphra/ZONOS3-Mini on GitHub at PIN_CODE (MIT), weights Zyphra/ZONOS3-Mini on Hugging Face at PIN_MODEL (Apache-2.0 per the model card)."""
import hashlib, io, json, os, pathlib, re, shutil, struct, subprocess, sys, time, urllib.error, urllib.request, wave

PIN_CODE = "96d9e0ec1e39083ed336ac78f24e3dbc93721c0f"   # GitHub main on 2026-11-14
PIN_MODEL = "d2d142a86b1e6aeb4252679673c86ecd86177bf8"  # Hugging Face main on 2026-11-14
REPO_URL = "https://github.com/Zyphra/ZONOS3-Mini.git"
MODEL_REPO = "Zyphra/ZONOS3-Mini"
FILES = ["gguf/backbone-ptq.gguf", "gguf/zac-keep2.gguf", "gguf/spk-encoder.gguf"]
VOICES = {"Ananya": ("default_f76cf78a50cf8496", "hi"), "Sravya": ("default_c3bb44b62cf13a5c", "te")}  # ids from gguf/voices/README.md
LANGS = {"hi": "hi_in", "ta": "ta_in", "te": "te_in", "ml": "ml_in", "kn": "kn_in"}
PORT = 1919
BASE = "http://127.0.0.1:%d" % PORT
WORK = pathlib.Path(os.environ.get("Z_WORK", "/content/zonos3_work"))
N = int(os.environ.get("Z_N", "2"))
_default_out = "/content/drive/MyDrive/zonos3_smoke" if os.path.isdir("/content/drive/MyDrive") else "/content/zonos3_smoke"
OUT = pathlib.Path(os.environ.get("Z_OUT", _default_out))


def say(*a):
    print(*a, flush=True)


def stop(msg, code=2):
    say("\nSTOP: " + msg)
    sys.exit(code)


def run(cmd, **kw):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, **kw)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", "not found: %s" % cmd[0])


def first_line(cmd):
    r = run(cmd)
    return (r.stdout or r.stderr).strip().splitlines()[0] if r.returncode == 0 and (r.stdout or r.stderr).strip() else None


def stream(cmd, log, cwd=None, env=None, label="", every=90):
    """Run a long command with output in a log file; print a heartbeat. Returns the exit code."""
    t0 = time.time(); nxt = every
    with open(log, "w") as fh:
        p = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=fh, stderr=subprocess.STDOUT)
        while p.poll() is None:
            time.sleep(5)
            if time.time() - t0 >= nxt:
                say("  [%s] still running, %.0f s" % (label, time.time() - t0)); nxt += every
    return p.returncode


def tail(path, n=40):
    try:
        return "\n".join(open(path, errors="replace").read().splitlines()[-n:])
    except OSError:
        return "(no log)"


# ---------- WAV and HTTP helpers (tested against a fake server) ----------
def wav_info(data):
    with wave.open(io.BytesIO(data), "rb") as w:
        ch, sw, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    info = {"channels": ch, "sample_width": sw, "rate": rate, "frames": n, "seconds": n / rate if rate else 0.0, "peak": 0, "rms_dbfs": None}
    if sw == 2 and n:
        s = struct.unpack("<%dh" % (len(raw) // 2), raw)
        info["peak"] = max(abs(v) for v in s)
        ms = sum(v * v for v in s) / len(s)
        info["rms_dbfs"] = round(10 * __import__("math").log10(ms / 32768.0 ** 2), 1) if ms > 0 else None
    return info


def http(method, path, body=None, timeout=120):
    req = urllib.request.Request(BASE + path, data=None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8"), method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def generate_one(text, language, voice_id, seed=1):
    t0 = time.perf_counter()
    status, headers, data = http("POST", "/v2/generate", {"text": text, "language": language, "speaker_embedding_id": voice_id,
                                                           "stream": False, "format": "wav", "seed": seed})
    dt = time.perf_counter() - t0
    if status != 200:
        return {"error": "HTTP %d: %s" % (status, data[:400].decode("utf-8", "replace")), "gen_s": dt}, None
    try:
        info = wav_info(data)
    except Exception as e:
        return {"error": "not a readable WAV: %s (first bytes %r)" % (e, data[:12]), "gen_s": dt}, None
    info.update(gen_s=dt, x_seed=headers.get("X-Seed"), x_decoder=headers.get("X-Decoder"), error="")
    info["rtf"] = dt / info["seconds"] if info["seconds"] else None
    return info, data


def main():
    say("ZONOS3-Mini smoke test. Pins: code %s, weights %s" % (PIN_CODE[:10], PIN_MODEL[:10]))
    if not os.environ.get("Z_OUT") and not os.path.isdir("/content/drive/MyDrive"):
        stop("Google Drive is not mounted and Z_OUT is not set: the results would be lost when the runtime is recycled. Mount Drive (from google.colab import drive; drive.mount('/content/drive')) or set Z_OUT, then run again.")
    bad = [k for k in os.environ if "TOKEN" in k.upper() and k != "COLAB_JUPYTER_TOKEN"]
    if bad:
        stop("token variable(s) set: %s. This test needs none: delete them and restart." % bad, 2)
    OUT.mkdir(parents=True, exist_ok=True); WORK.mkdir(parents=True, exist_ok=True)
    env_info = {"pins": {"code": PIN_CODE, "weights": PIN_MODEL}, "python": sys.version.split()[0]}

    # ---- 1. environment: any problem here is an environment problem, not a model result ----
    gpu = run(["nvidia-smi", "--query-gpu=name,memory.total,compute_cap", "--format=csv,noheader"])
    if gpu.returncode != 0:
        stop("no GPU: Runtime > Change runtime type > GPU, then restart. (Environment problem.)")
    name, mem, cc = [x.strip() for x in gpu.stdout.strip().splitlines()[0].split(",")]
    cc = cc.replace(".", "") if re.fullmatch(r"\d\.\d", cc) else ("75" if "T4" in name else "")
    if not cc:
        stop("cannot work out the CUDA architecture of %s: set Z_CUDA_ARCH, e.g. 75 for a T4." % name) if not os.environ.get("Z_CUDA_ARCH") else None
    cc = os.environ.get("Z_CUDA_ARCH", cc)
    env_info.update(gpu=name, gpu_memory=mem, cuda_arch=cc)
    say("GPU: %s, %s, architecture %s" % (name, mem, cc))
    nvcc = first_line(["nvcc", "--version"]) and run(["nvcc", "--version"]).stdout.strip().splitlines()[-1]
    if not nvcc:
        stop("nvcc (the CUDA toolkit) was not found on this runtime, so the CUDA build cannot start. (Environment problem; "
             "this says nothing about the model.)")
    cm = first_line(["cmake", "--version"]); gcc = first_line(["g++", "--version"]); git = first_line(["git", "--version"])
    ver = re.search(r"(\d+)\.(\d+)", cm or "")
    if not (cm and gcc and git and ver and (int(ver.group(1)), int(ver.group(2))) >= (3, 18)):
        stop("need git, g++ and cmake >= 3.18; found: git=%s, g++=%s, cmake=%s. (Environment problem.)" % (git, gcc, cm))
    free = shutil.disk_usage(str(WORK)).free / 2 ** 30
    if free < 15:
        stop("only %.1f GiB free in %s; need about 15. (Environment problem.)" % (free, WORK))
    env_info.update(nvcc=nvcc, cmake=cm, gcc=gcc, git=git, free_gib=round(free, 1))
    say("nvcc: %s | %s | %s | free disk %.0f GiB" % (nvcc, cm, gcc, free))

    # ---- 2. Rust (the text normaliser is built with cargo) ----
    os.environ["PATH"] = os.path.expanduser("~/.cargo/bin") + os.pathsep + os.environ["PATH"]
    if not first_line(["cargo", "--version"]):
        say("installing a minimal stable Rust toolchain with rustup ...")
        rc = stream(["bash", "-c", "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs -o /tmp/rustup.sh && sh /tmp/rustup.sh -y --profile minimal"],
                    str(WORK / "rustup.log"), label="rustup")
        if rc != 0 or not first_line(["cargo", "--version"]):
            stop("Rust install failed (environment problem):\n" + tail(WORK / "rustup.log", 15), 3)
    env_info["rustc"] = first_line(["rustc", "--version"]); say(env_info["rustc"])

    # ---- 2b. optional build cache (opt-in: Z_CACHE is a folder on Drive) ----
    cache_file = None
    if os.environ.get("Z_CACHE"):
        cache_dir = pathlib.Path(os.environ["Z_CACHE"]); cache_dir.mkdir(parents=True, exist_ok=True)
        key = "%s_sm%s_%s" % (PIN_CODE[:10], cc, hashlib.sha1(str(nvcc).encode()).hexdigest()[:8])
        cache_file = cache_dir / ("zonos3_build_%s.tar" % key)
        if cache_file.exists() and not (WORK / "zonos3" / ".git").exists():
            say("restoring the cached build %s (%.0f MiB) ..." % (cache_file.name, cache_file.stat().st_size / 2 ** 20))
            r = run(["tar", "-xf", str(cache_file), "-C", str(WORK)])
            if r.returncode:
                say("cache restore failed, the server will be built instead: " + r.stderr[-200:])
    else:
        say("Z_CACHE is not set: the build will not be cached. Set it to a folder on Drive to skip the hour-long build next time.")

    # ---- 3. code at the pinned commit, only the submodules the server needs ----
    src = WORK / "zonos3"
    if not (src / ".git").exists():
        r = run(["git", "clone", "-q", REPO_URL, str(src)])
        if r.returncode: stop("git clone failed (network?): " + r.stderr[-300:], 3)
    for args in (["checkout", "-q", PIN_CODE], ["submodule", "update", "--init", "servers/ggml/ggml", "third_party/zyphra-textnorm"]):
        r = run(["git", "-C", str(src)] + args)
        if r.returncode: stop("git %s failed: %s" % (" ".join(args[:2]), r.stderr[-400:]), 3)
    env_info["submodules"] = run(["git", "-C", str(src), "submodule", "status"]).stdout.strip().splitlines()
    say("submodules:\n  " + "\n  ".join(env_info["submodules"]))

    # ---- 4. build ----
    ggml = src / "servers" / "ggml"
    exe = next(iter(ggml.glob("build-cuda/zonos3-server")), None)
    if exe is None:
        say("building the CUDA server (this takes a while; architecture %s) ..." % cc)
        t0 = time.time()
        rc = stream(["cmake", "-B", "build-cuda", "-DGGML_CUDA=ON", "-DCMAKE_BUILD_TYPE=Release", "-DCMAKE_CUDA_ARCHITECTURES=" + cc],
                    str(WORK / "cmake_configure.log"), cwd=str(ggml), label="configure")
        if rc != 0:
            stop("BUILD FAILED at configure (not a result about the model). Last lines:\n" + tail(WORK / "cmake_configure.log"), 3)
        rc = stream(["cmake", "--build", "build-cuda", "-j", str(os.cpu_count() or 2)], str(WORK / "cmake_build.log"), cwd=str(ggml), label="build", every=120)
        if rc != 0:
            stop("BUILD FAILED (not a result about the model). Last lines:\n" + tail(WORK / "cmake_build.log"), 3)
        env_info["build_seconds"] = round(time.time() - t0)
        exe = next(iter(ggml.glob("build-cuda/**/zonos3-server")), None)
    if exe is None:
        stop("the build finished but no zonos3-server binary was found under %s/build-cuda." % ggml, 3)
    say("server binary:", exe)
    if cache_file is not None and not cache_file.exists():
        say("saving the build to %s ..." % cache_file)
        r = run(["tar", "-cf", str(cache_file) + ".part", "-C", str(WORK), "zonos3"])
        if r.returncode == 0:
            os.replace(str(cache_file) + ".part", str(cache_file))
            say("saved, %.0f MiB" % (cache_file.stat().st_size / 2 ** 20))
        else:
            say("cache save failed (not a problem for this run): " + r.stderr[-200:])

    # ---- 5. weights at the pinned revision, with their SHA-256 ----
    r = run([sys.executable, "-m", "pip", "install", "-q", "huggingface_hub", "datasets==3.6.0"])
    if r.returncode: stop("pip install failed: " + r.stderr[-300:], 3)
    from huggingface_hub import snapshot_download
    mdir = WORK / "model"
    snapshot_download(MODEL_REPO, revision=PIN_MODEL, allow_patterns=FILES + ["gguf/voices/*"], local_dir=str(mdir))
    env_info["files"] = {}
    for f in FILES:
        p = mdir / f
        if not p.exists(): stop("expected file missing after download: " + f, 3)
        h = hashlib.sha256()
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 22), b""):
                h.update(chunk)
        env_info["files"][f] = {"bytes": p.stat().st_size, "sha256": h.hexdigest()}
        say("  %-26s %8.1f MiB  sha256 %s" % (f, p.stat().st_size / 2 ** 20, h.hexdigest()))

    # ---- 6. prompts: FLEURS sentence text only ----
    from datasets import load_dataset
    prompts = {}
    for lang, cfg in LANGS.items():
        ds = load_dataset("google/fleurs", cfg, split="test", streaming=True, trust_remote_code=True).remove_columns(["audio"])
        seen = []
        for ex in ds:
            t = ex["raw_transcription"].strip()
            if t in seen or re.search(r"\d", t) or re.search(r"[A-Za-z]", t) or not 6 <= len(t.split()) <= 30:
                continue
            seen.append(t)
            if len(seen) >= N: break
        prompts[lang] = seen
        say("  %s: %d prompts" % (lang, len(seen)))

    # ---- 7. start the server (loopback only) ----
    log = WORK / "server.log"
    proc = None; used = None
    for flags in (["--gpu", "--host", "127.0.0.1"], ["--gpu"], []):
        cmd = [str(exe), "gguf/backbone-ptq.gguf", "--tts-default-voices-dir", "gguf/voices", "--port", str(PORT)] + flags
        say("starting:", " ".join(cmd))
        fh = open(log, "w")
        proc = subprocess.Popen(cmd, cwd=str(mdir), stdout=fh, stderr=subprocess.STDOUT)
        ok = False
        for _ in range(240):           # up to 20 minutes
            time.sleep(5)
            if proc.poll() is not None:
                break
            try:
                if http("GET", "/v2/health", timeout=5)[0] == 200:
                    ok = True; break
            except Exception:
                pass
        if ok:
            used = flags; break
        say("  server did not come up with flags %s (exit %s). Last log lines:\n%s" % (flags, proc.poll(), tail(log, 12)))
        if proc.poll() is None: proc.terminate()
    if used is None:
        stop("the server never answered /v2/health (see the log lines above; not a result about the model).", 3)
    env_info["server_flags"] = used
    if "--gpu" not in used:
        say("WARNING: the server only started without --gpu, so it may run on the CPU; timings below are not GPU timings.")
    try:
        health = http("GET", "/v2/health")[2].decode(); caps = json.loads(http("GET", "/v2/capabilities")[2])
        speakers = json.loads(http("GET", "/v2/speakers")[2])
    except Exception as e:
        proc.terminate(); stop("server answered health but not capabilities/speakers: %s" % e, 3)
    say("health:", health.strip()); say("capabilities (keys):", sorted(caps.keys()) if isinstance(caps, dict) else caps)
    sp = speakers.get("speakers", speakers) if isinstance(speakers, dict) else speakers
    ids = {s.get("id") for s in sp if isinstance(s, dict)}
    say("speakers listed: %d; wanted ids present: %s" % (len(ids), {n: v[0] in ids for n, v in VOICES.items()}))
    env_info["capabilities"] = caps
    missing = [n for n, v in VOICES.items() if v[0] not in ids]
    if missing:
        proc.terminate(); stop("stock voice ids not found in /v2/speakers: %s. First entries: %s" % (missing, sp[:3]), 3)

    # ---- 8. generate ----
    rows = []
    for lang, texts in prompts.items():
        for vname, (vid, vlang) in VOICES.items():
            for k, text in enumerate(texts):
                info, data = generate_one(text, lang, vid)
                tag = "%s_%s_%02d" % (lang, vname, k)
                if data:
                    (OUT / (tag + ".wav")).write_bytes(data)
                row = {"id": tag, "lang": lang, "voice": vname, "voice_language": vlang, "voice_matches_text_language": vlang == lang,
                       "chars": len(text), "text": text, **info}
                rows.append(row)
                say("  %-18s %s audio %.1fs gen %.1fs rtf %s peak %s rms %s dBFS %s" % (
                    tag, "ok " if not info["error"] else "ERR", info.get("seconds", 0), info["gen_s"],
                    "%.2f" % info["rtf"] if info.get("rtf") else "-", info.get("peak"), info.get("rms_dbfs"), info["error"][:200]))
    proc.terminate()
    ok_rows = [r for r in rows if not r["error"]]
    silent = [r["id"] for r in ok_rows if r["peak"] < 100]
    cps = {r["id"]: r["chars"] / r["seconds"] for r in ok_rows if r.get("seconds")}
    med = sorted(cps.values())[len(cps) // 2] if cps else 0
    fast = sorted(k for k, v in cps.items() if med and v > 1.8 * med)
    say("characters per second: median %.1f; more than 1.8 times the median (the speech may be cut or rushed): %s" % (med, fast or "none"))
    summary = {"env": env_info, "rows": rows, "clips_ok": len(ok_rows), "clips_total": len(rows), "silent_clips": silent}
    (OUT / "zonos3_smoke.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    say("\nclips ok: %d of %d; silent (peak < 100): %s; results and WAVs in %s" % (len(ok_rows), len(rows), silent or "none", OUT))
    say("Limits: one run, %d sentences per language, two stock voices, seed 1; nothing here says whether the speech is intelligible, natural,"
        " in the right accent or expressive. That needs the round-trip script and human listeners. Do not commit the WAV files." % N)


if __name__ == "__main__":
    main()
