"""Worker script of the ZONOS3-Mini engine (docs/design/zonos3-engine.md). Standard library only.

It starts the operator-built zonos3-server (found on the PATH of this worker) on 127.0.0.1 from the verified model folder, talks
to it over HTTP and stops it in close. The server binary is not covered by any lock file. Only the stock voices of the table
below are used: no speaker audio, no embedding upload, no cloning (consent design: issue #763).
"""
import http.client
import importlib.util
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import wave

try:
    import ctypes
    _LIBC = ctypes.CDLL(None)
except (ImportError, OSError):
    _LIBC = None

MODEL = "zonos3-mini"
BINARY = "zonos3-server"
FILES = {"backbone": "gguf/backbone-ptq.gguf", "zac": "gguf/zac-keep2.gguf", "spk": "gguf/spk-encoder.gguf",
         "voices": "gguf/voices"}
VOICES = {"hi": ("Ananya", "default_f76cf78a50cf8496"), "te": ("Sravya", "default_c3bb44b62cf13a5c")}
_OPTIONS = ("seed",)
START_TIMEOUT = 600.0
RUN_TIMEOUT = 280.0
MAX_CHARS = 2000  # a starting value, not measured
MAX_WAV = 100 * 1024 * 1024
_state = {}


def _free_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _die_with_parent():
    # Linux only (PR_SET_PDEATHSIG = 1): the kernel kills the server if this worker dies, so no server is left running.
    if _LIBC is not None and sys.platform.startswith("linux"):
        _LIBC.prctl(1, int(signal.SIGKILL))


def _call(method, path, body=None, timeout=10.0, limit=1 << 20):
    conn = http.client.HTTPConnection("127.0.0.1", _state["port"], timeout=timeout)
    try:
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read(limit + 1)
        if len(data) > limit:
            raise RuntimeError("the answer of the server is too large.")
        return resp.status, data
    finally:
        conn.close()


def _tail(limit=400):
    try:
        with open(_state["log"], "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - limit))
            text = f.read().decode("utf-8", "replace")
    except (KeyError, OSError):
        return "(no log)"
    return " | ".join(line.strip() for line in text.splitlines() if line.strip()) or "(empty)"


def _stop():
    proc = _state.get("proc")
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    log = _state.get("log")
    if log:
        try:
            os.unlink(log)
        except OSError:
            pass
    _state.clear()


def _wait_until_up(proc):
    deadline = time.monotonic() + START_TIMEOUT
    while True:
        code = proc.poll()
        if code is not None:
            raise RuntimeError("the server exited with code %s while starting. Log tail: %s" % (code, _tail()))
        try:
            status, _ = _call("GET", "/v2/health", timeout=3.0)
            if status == 200:
                return
        except (OSError, http.client.HTTPException):
            pass
        if time.monotonic() > deadline:
            raise RuntimeError("the server did not answer within %d s. Log tail: %s" % (START_TIMEOUT, _tail()))
        time.sleep(0.25)


def _check_voices():
    status, data = _call("GET", "/v2/speakers", timeout=10.0)
    try:
        listed = {s.get("id") for s in json.loads(data.decode("utf-8")).get("speakers", [])}
    except (ValueError, AttributeError, TypeError):
        raise RuntimeError("the speaker list of the server is not readable (HTTP %d)." % status)
    missing = sorted(name for name, sid in VOICES.values() if sid not in listed)
    if missing:
        raise RuntimeError("the server does not list the stock voice(s) %s: the clips and their .npy files must be in %s."
                           % (", ".join(missing), FILES["voices"]))


def load(msg):
    _stop()
    root = (msg.get("model_dirs") or {}).get(MODEL)
    if not root:
        raise RuntimeError("the folder of %s is needed." % MODEL)
    paths = {k: os.path.join(root, *v.split("/")) for k, v in FILES.items()}
    missing = [FILES[k] for k in FILES if not os.path.exists(paths[k])]
    if missing:
        raise RuntimeError("the model folder lacks: %s." % ", ".join(missing))
    binary = shutil.which(BINARY)
    if not binary:
        raise RuntimeError("%s is not on the PATH. Build it from the model's own repository (it is not covered by the lock) "
                           "and put it on the PATH of the process that starts the engine." % BINARY)
    port = _free_port()
    fd, log = tempfile.mkstemp(prefix="zonos3-server-", suffix=".log")
    cmd = [binary, paths["backbone"], "--zac", paths["zac"], "--spk-encoder", paths["spk"], "--cpu",
           "--host", "127.0.0.1", "--port", str(port), "--tts-default-voices-dir", paths["voices"]]
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=fd, stderr=subprocess.STDOUT,
                                preexec_fn=_die_with_parent if os.name == "posix" else None)
    finally:
        os.close(fd)
    _state.update(port=port, proc=proc, log=log)
    try:
        _wait_until_up(proc)
        _check_voices()
    except BaseException:
        _stop()
        raise
    return {}


def run(msg):
    proc = _state.get("proc")
    if proc is None:
        raise RuntimeError("the model is not loaded.")
    if proc.poll() is not None:
        raise RuntimeError("the server has exited (code %s). Log tail: %s" % (proc.returncode, _tail()))
    req = msg["request"]
    text = req.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must be a non-empty string")
    if len(text) > MAX_CHARS:
        raise ValueError("text is longer than %d characters" % MAX_CHARS)
    language = req.get("language")
    if language not in VOICES:
        raise ValueError("language must be one of: %s" % ", ".join(sorted(VOICES)))
    options = req.get("options") or {}
    unknown = sorted(set(options) - set(_OPTIONS))
    if unknown:
        raise ValueError("unsupported option(s): %s (supported: %s)" % (", ".join(unknown), ", ".join(_OPTIONS)))
    seed = options.get("seed", 1)
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2 ** 31:
        raise ValueError("seed must be an integer from 0 to 2147483647")
    name, speaker_id = VOICES[language]
    body = json.dumps({"text": text, "language": language, "speaker_embedding_id": speaker_id, "stream": False,
                       "format": "wav", "seed": seed}).encode("utf-8")
    status, data = _call("POST", "/v2/generate", body=body, timeout=RUN_TIMEOUT, limit=MAX_WAV)
    if status != 200:
        raise RuntimeError("the server answered HTTP %d: %s" % (status, data[:200].decode("utf-8", "replace").strip()))
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise RuntimeError("the answer of the server is not a WAV file.")
    out = os.path.join(msg["work_dir"], "out.wav")
    with open(out, "wb") as f:
        f.write(data)
    with wave.open(out, "rb") as w:
        rate = w.getframerate()
    return {"wav_path": out, "sample_rate": int(rate),
            "notes": ["voice: %s (a stock voice of the model; language %s; seed %d)." % (name, language, seed),
                      "the server ran on the CPU (--cpu); speed and emotion are not controlled.",
                      "naturalness is not measured by listening; the origin of the stock voice recordings is not recorded."]}


def close(msg):
    _stop()


def _runtime():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "worker_runtime.py")
    spec = importlib.util.spec_from_file_location("worker_runtime", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


if __name__ == "__main__":
    # the package folder holds modules (api, cache, coverage, ...) that could shadow libraries in this environment
    _here = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != _here]
    _runtime().serve({"load": load, "run": run, "close": close})
