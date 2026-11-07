"""Was the GPU used? Runs a Transcriber on cpu, cuda and auto and samples nvidia-smi during three warm calls (Colab GPU).
Needs: pip install "ragleap-voice[whisper]" datasets==3.6.0 numpy nvidia-cublas-cu12 nvidia-cudnn-cu12, and the model already pulled.
The CUDA 12 libraries are preloaded with ctypes before the model loads. No Hugging Face token should be set.
The cpu run is the control: it should show no GPU memory growth and no GPU utilisation.
One run, one clip, one machine. Memory and utilisation come from nvidia-smi samples every 0.1 s: a heuristic, not a profiler."""
import ctypes, glob, io, json, os, site, subprocess, threading, time, wave
import numpy as np
from datasets import load_dataset

for sp in site.getsitepackages():
    for pat in ("nvidia/cublas/lib/libcublas.so.12*", "nvidia/cudnn/lib/libcudnn*.so*"):
        for p in sorted(glob.glob(os.path.join(sp, pat))):
            try:
                ctypes.CDLL(p, mode=ctypes.RTLD_GLOBAL)
            except OSError:
                pass

import ragleap_voice
from ragleap_voice import Transcriber


def smi():
    out = subprocess.check_output(["nvidia-smi", "--query-gpu=memory.used,utilization.gpu",
                                   "--format=csv,noheader,nounits"]).decode().strip().split(",")
    return int(out[0]), int(out[1])


def clip_wav():
    for ex in load_dataset("google/fleurs", "en_us", split="test", streaming=True, trust_remote_code=True):
        x = np.asarray(ex["audio"]["array"], dtype=np.float64)
        pcm = np.clip(np.round(x * 32767), -32768, 32767).astype("<i2")
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(pcm.tobytes())
        return buf.getvalue()


class Sampler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.stop_flag = threading.Event()
        self.mem, self.util = [], []

    def run(self):
        while not self.stop_flag.is_set():
            m, u = smi()
            self.mem.append(m); self.util.append(u)
            time.sleep(0.1)


wav = clip_wav()


def measure(device):
    res = {"device": device, "mem_before_mib": smi()[0]}
    opts = {"device": device}
    with Transcriber() as t:
        try:
            t.transcribe(wav, language="en", allow_unverified=True, options=opts)
        except Exception as e:
            res["error"] = type(e).__name__ + ": " + str(e)[:150]
            return res
        res["mem_after_load_mib"] = smi()[0]
        s = Sampler(); s.start()
        t0 = time.perf_counter()
        for _ in range(3):
            t.transcribe(wav, language="en", allow_unverified=True, options=opts)
        res["seconds_3_warm_calls"] = round(time.perf_counter() - t0, 2)
        s.stop_flag.set(); s.join()
        res["samples"] = len(s.util)
        res["max_gpu_util_pct"] = max(s.util)
        res["mean_gpu_util_pct"] = round(sum(s.util) / len(s.util), 1)
        res["max_mem_mib"] = max(s.mem)
    time.sleep(1)
    res["mem_after_close_mib"] = smi()[0]
    return res


gpu = subprocess.check_output(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"]).decode().strip()
results = [measure(d) for d in ("cpu", "cuda", "auto")]
print(json.dumps({"ragleap_voice": ragleap_voice.__version__, "gpu": gpu, "results": results}, indent=1))
print("Limits: one run, one clip, one machine; nvidia-smi samples are a heuristic, not a profiler.")
