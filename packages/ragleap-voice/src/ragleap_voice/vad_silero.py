"""Optional model-based voice-activity detector: Silero VAD (ONNX), with the same events as StreamGate (docs/design/vad-gate.md, section 9).

Status: pre-alpha, not exported, off by default. The model file is NOT bundled and is never downloaded: the caller gives its path, and
the file must have the pinned SHA-256 below or it is refused. Needs the optional extra: pip install "ragleap-voice[vad]" (onnxruntime and
numpy are imported only when the detector is built). The rate must be 16000. Not thread-safe.
Thresholds: probability 0.5 and three consecutive windows are the values used in benchmarks/voice (colab_gate_real_noise_benchmark.py),
which measured false accepts on noise only, not speech detection. The hangover and hard-cap defaults were not measured at all.
Model: silero_vad_16k_op15.onnx at snakers4/silero-vad commit 066409f3d9db45a30d21dfcbf9147826ffdd7052. The repository is MIT; the
licence of the weights is known here only from the repository's README wording, and the training data is not documented here: unverified.
"""
import array
import hashlib
import importlib
import math
import pathlib

from .errors import EngineNotInstalledError, ModelNotFoundError, ModelNotVerifiedError, UnsupportedFeatureError
from .gate import StreamGateEvent

MODEL_SHA256 = "7ed98ddbad84ccac4cd0aeb3099049280713df825c610a8ed34543318f1b2c49"
RATE = 16000
WINDOW = 512
CONTEXT = 64
WINDOW_MS = WINDOW * 1000 // RATE  # 32
DEFAULTS = {"threshold": 0.5, "min_speech_windows": 3, "hangover_windows": 12, "hard_cap_windows": 940}
_INSTALL = 'pip install "ragleap-voice[vad]"'


def _import(name):
    return importlib.import_module(name)


class _OrtBackend:
    """Runs the model window by window and keeps its recurrent state and the 64 sample context."""

    def __init__(self, raw):
        try:
            ort = _import("onnxruntime")
            self._np = _import("numpy")
        except ImportError:
            raise EngineNotInstalledError("onnxruntime and numpy are needed for the Silero detector and are not installed.",
                                          hint="install the extra: " + _INSTALL)
        np = self._np
        self._sess = ort.InferenceSession(raw, providers=["CPUExecutionProvider"])
        self._state = np.zeros((2, 1, 128), np.float32)
        self._ctx = np.zeros(CONTEXT, np.float32)

    def prob(self, window):
        np = self._np
        x = np.asarray(window, dtype=np.float32) / 32768.0
        p, self._state = self._sess.run(None, {"input": np.concatenate([self._ctx, x])[None, :], "state": self._state,
                                               "sr": np.array(RATE, np.int64)})
        self._ctx = x[-CONTEXT:]
        return float(p[0, 0])


def _build_backend(raw):
    return _OrtBackend(raw)


def _load_backend(model_path):
    p = pathlib.Path(model_path)
    if not p.is_file():
        raise ModelNotFoundError("the Silero model file %s does not exist." % p,
                                 hint="supply the path of silero_vad_16k_op15.onnx; nothing is downloaded for you.")
    raw = p.read_bytes()
    got = hashlib.sha256(raw).hexdigest()
    if got != MODEL_SHA256:
        raise ModelNotVerifiedError("%s does not have the pinned SHA-256 (expected %s..., got %s...) and was refused."
                                    % (p.name, MODEL_SHA256[:12], got[:12]),
                                    hint="use silero_vad_16k_op15.onnx from snakers4/silero-vad at the commit named in vad_silero.py.")
    return _build_backend(raw)


def _num(value, name, lo, hi):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo < value < hi:
        raise UnsupportedFeatureError("%s must be a number between %s and %s (exclusive)." % (name, lo, hi))
    return float(value)


def _count(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise UnsupportedFeatureError("%s must be an integer of at least 1." % name)
    return value


class SileroGate:
    """Window-by-window speech detector with the StreamGate interface: feed() and finish() return StreamGateEvent objects.
    One frame is one 512 sample window (32 ms), so event frame numbers count windows."""

    def __init__(self, rate, model_path, options=None, *, _backend=None):
        if isinstance(rate, bool) or not isinstance(rate, int) or rate != RATE:
            raise UnsupportedFeatureError("SileroGate needs rate 16000.")
        opts = dict(DEFAULTS)
        user = dict(options or {})
        unknown = sorted(set(user) - set(DEFAULTS))
        if unknown:
            raise UnsupportedFeatureError("unknown option(s): %s." % ", ".join(unknown), hint="supported: %s." % ", ".join(sorted(DEFAULTS)))
        opts.update(user)
        self._thr = _num(opts["threshold"], "threshold", 0.0, 1.0)
        self._min = _count(opts["min_speech_windows"], "min_speech_windows")
        self._hang = _count(opts["hangover_windows"], "hangover_windows")
        self._cap = _count(opts["hard_cap_windows"], "hard_cap_windows")
        if self._cap <= self._min:
            raise UnsupportedFeatureError("hard_cap_windows must be larger than min_speech_windows.")
        self._backend = _backend if _backend is not None else _load_backend(model_path)
        self._buf = array.array("h")
        self._index = 0
        self._in_speech = False
        self._run = 0
        self._silence = 0
        self._last_speech = 0
        self._start = 0
        self._started = False
        self._done = False

    def _event(self, kind, frame, reason):
        return StreamGateEvent(kind, frame, frame * WINDOW_MS / 1000.0, reason)

    def _step(self, prob):
        i = self._index
        self._index += 1
        speech = prob >= self._thr
        events = []
        if not self._in_speech:
            self._run = self._run + 1 if speech else 0
            if self._run >= self._min:
                self._in_speech, self._started = True, True
                self._start, self._last_speech, self._silence = i - self._run + 1, i, 0
                events.append(self._event("speech_start", self._start, "%d consecutive speech windows" % self._run))
            return events
        if speech:
            self._last_speech, self._silence = i, 0
        else:
            self._silence += 1
        if self._silence >= self._hang:
            events.append(self._event("speech_end", self._last_speech + 1, "silence"))
        elif i - self._start + 1 >= self._cap:
            events.append(self._event("speech_end", i + 1, "hard_cap"))
        if events:
            self._in_speech, self._run = False, 0
        return events

    def feed(self, samples):
        """Accept a chunk of 16-bit samples (array('h') or ints); return the events it completed."""
        if self._done:
            raise ValueError("feed() called after finish()")
        self._buf.extend(samples)
        events, pos = [], 0
        while len(self._buf) - pos >= WINDOW:
            events.extend(self._step(self._backend.prob(self._buf[pos:pos + WINDOW])))
            pos += WINDOW
        del self._buf[:pos]
        return events

    def finish(self):
        """End the stream. Closes an open utterance, or reports no_speech if none ever started. Later calls return []."""
        if self._done:
            return []
        self._done = True
        if self._in_speech:
            return [self._event("speech_end", self._index, "stream_end")]
        if not self._started:
            return [self._event("no_speech", self._index, "no utterance reached %d consecutive speech windows" % self._min)]
        return []
