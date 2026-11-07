"""Per-call timing of transcribe() and Transcriber on one clip (Colab GPU, Whisper large-v3). Not an accuracy benchmark.
Needs: pip install "ragleap-voice[whisper]" datasets==3.6.0 numpy, and the model already pulled
(ragleap-voice models pull --help shows the arguments). If the CUDA libraries are not found, preload them first.
No Hugging Face token should be set when this runs. Set TIMING_DEVICE to auto, cpu or cuda (default auto).
One run, one clip, one machine: the numbers describe that setup only. No comparison with any other product is made.
The file cache of the operating system is warm after the first mode, so the second mode does not show a cold disk read."""
import io, json, os, platform, subprocess, time, wave
import numpy as np
from datasets import load_dataset
import ragleap_voice
from ragleap_voice import Transcriber, transcribe

DEVICE = os.environ.get("TIMING_DEVICE", "auto")
OPTS = {"device": DEVICE}
OUT = os.environ.get("TIMING_OUT", "transcribe_timing.json")


def clip_wav():
    for ex in load_dataset("google/fleurs", "en_us", split="test", streaming=True, trust_remote_code=True):
        x = np.asarray(ex["audio"]["array"], dtype=np.float64)
        pcm = np.clip(np.round(x * 32767), -32768, 32767).astype("<i2")
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(pcm.tobytes())
        return buf.getvalue(), len(pcm) / 16000


def gpu():
    try:
        return subprocess.check_output(["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"]).decode().strip()
    except Exception:
        return "no nvidia-smi"


wav, dur = clip_wav()
rows = []


def record(mode, fn, n):
    for i in range(1, n + 1):
        t = time.perf_counter()
        r = fn()
        s = time.perf_counter() - t
        rows.append({"mode": mode, "call": i, "seconds": round(s, 3), "rtf": round(s / dur, 3), "chars": len(r.text)})


def call():
    return transcribe(wav, language="en", engine="whisper", allow_unverified=True, options=OPTS)


record("transcribe() default", call, 4)
os.environ["RAGLEAP_VOICE_VERIFY"] = "always"
record("transcribe() verify=always", call, 2)
os.environ.pop("RAGLEAP_VOICE_VERIFY", None)
with Transcriber() as t:
    record("Transcriber", lambda: t.transcribe(wav, language="en", allow_unverified=True, options=OPTS), 4)

env = {"ragleap_voice": ragleap_voice.__version__, "python": platform.python_version(), "gpu": gpu(),
       "device_option": DEVICE, "clip_seconds": round(dur, 2), "clip": "google/fleurs en_us test, first clip", "runs": 1}
print(json.dumps(env, indent=1))
print("%-28s %4s %9s %6s %6s" % ("mode", "call", "seconds", "RTF", "chars"))
for r in rows:
    print("%-28s %4d %9.3f %6.3f %6d" % (r["mode"], r["call"], r["seconds"], r["rtf"], r["chars"]))
json.dump({"env": env, "rows": rows}, open(OUT, "w"), indent=1)
print("saved", OUT)
print("Limits: one clip, one run, one machine; timing of batch decoding only, not streaming latency; not a comparison with other systems.")
