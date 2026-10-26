"""Whisper large-v3 speech-to-text engine on faster-whisper (CTranslate2). docs/design/api.md sections 4, 5 and 7.

The core stays standard library only: faster_whisper and numpy are imported when the engine loads or runs. The engine is
handed a verified directory and never fetches anything; it is told to use local files only. It decodes the WAV itself and
passes samples to faster-whisper, so PyAV is not used for our input.
"""
import contextlib
import io
import os
import wave

from .engine import Capabilities, Result
from .errors import (AudioDecodeError, EngineError, EngineNotInstalledError, EngineUnavailableError,
                     UnsupportedFeatureError)
from .results import Segment

MODEL = "whisper-large-v3"
# Languages with a usable measured result in coverage/stt.csv (60 read-speech FLEURS clips each, en only 20).
LANGUAGES = frozenset({"es", "fr", "pt", "ru", "tr", "id", "vi", "en", "ja"})
RATE = 16000
_OFFLINE = {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
_INSTALL = 'pip install "ragleap-voice[whisper]"'
_OPTIONS = ("beam_size", "device")
# device -> compute type. cpu/int8 ran on Colab; cuda/float16 has not run yet.
_DEVICES = {"auto": "default", "cpu": "int8", "cuda": "float16"}
_CUDA_WORDS = ("libcublas", "libcudnn", "cuda")
_CUDA_HINT = ('pass options={"device": "cpu"} to run on the CPU, or install the CUDA 12 libraries that ctranslate2 needs. '
              'On one Colab T4 image, pip install nvidia-cublas-cu12 nvidia-cudnn-cu12 and loading their .so files with '
              'ctypes (RTLD_GLOBAL) before the model made device cuda work; this is one observation, not a guarantee.')


@contextlib.contextmanager
def _offline_env():
    old = {k: os.environ.get(k) for k in _OFFLINE}
    os.environ.update(_OFFLINE)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _cuda_check(exc):
    """Raise EngineUnavailableError when exc looks like a missing CUDA library or driver; otherwise return."""
    low = str(exc).lower()
    if any(w in low for w in _CUDA_WORDS):
        raise EngineUnavailableError("CUDA is not usable here: %s" % exc, hint=_CUDA_HINT) from exc


def _samples(audio):
    """Mono 16-bit 16 kHz PCM WAV bytes -> float32 samples in [-1, 1)."""
    try:
        import numpy
    except ImportError:
        raise EngineNotInstalledError("numpy is needed to hand audio to faster-whisper and is not installed.",
                                      hint="pip install numpy, or reinstall the extra: " + _INSTALL)
    try:
        with wave.open(io.BytesIO(audio), "rb") as w:
            channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
            if (channels, width, rate) != (1, 2, RATE):
                raise UnsupportedFeatureError(
                    "whisper needs mono 16-bit 16 kHz WAV, got %d channel(s), %d-bit, %d Hz." % (channels, width * 8, rate),
                    hint="convert first, for example: ffmpeg -i in.wav -ac 1 -ar 16000 -sample_fmt s16 out.wav "
                         "(resampling inside ragleap-voice is planned, not built).")
            frames = w.readframes(w.getnframes())
    except (wave.Error, EOFError) as exc:
        raise AudioDecodeError("not a readable PCM WAV file (%s)." % exc)
    return numpy.frombuffer(frames, dtype="<i2").astype("float32") / 32768.0


class WhisperEngine:
    name = "whisper"
    manifest_entries = (MODEL,)
    capabilities = Capabilities(kind="stt", languages=LANGUAGES, hardware=frozenset({"cpu", "cuda"}))

    def __init__(self):
        self._fw = None
        self._path = None
        self._device = None
        self._model = None

    def _open(self, device):
        try:
            with _offline_env():
                self._model = self._fw.WhisperModel(self._path, device=device, compute_type=_DEVICES[device],
                                                    local_files_only=True)
        except (RuntimeError, ValueError) as exc:
            _cuda_check(exc)
            raise
        self._device = device

    def load(self, model_dirs):
        try:
            import faster_whisper
        except ImportError:
            raise EngineNotInstalledError("the faster-whisper package is not installed.",
                                          hint="install the extra: " + _INSTALL)
        path = model_dirs.get(MODEL)
        if path is None:
            raise EngineError("the verified folder of %r was not provided." % MODEL)
        self._fw, self._path = faster_whisper, str(path)
        self._open("auto")

    def run(self, request):
        if self._model is None:
            raise EngineError("the whisper engine was not loaded.", hint="call load() before run().")
        unknown = sorted(set(request.options) - set(_OPTIONS))
        if unknown:
            raise UnsupportedFeatureError("whisper does not support option(s): %s." % ", ".join(unknown),
                                          hint="supported options: %s." % ", ".join(_OPTIONS))
        beam = request.options.get("beam_size", 5)
        if not isinstance(beam, int) or isinstance(beam, bool) or not 1 <= beam <= 10:
            raise UnsupportedFeatureError("beam_size must be an integer from 1 to 10.")
        device = request.options.get("device", "auto")
        if not isinstance(device, str) or device not in _DEVICES:
            raise UnsupportedFeatureError("device must be one of: %s." % ", ".join(_DEVICES))
        audio = _samples(request.audio)
        if device != self._device:
            self._model = None
            self._open(device)
        parts, segs = [], []
        try:
            segments, _info = self._model.transcribe(audio, language=request.language, beam_size=beam,
                                                     vad_filter=False)
            for s in segments:
                text = s.text.strip()
                parts.append(text)
                if request.timestamps:
                    segs.append(Segment(float(s.start), float(s.end), text))
        except (RuntimeError, ValueError) as exc:
            _cuda_check(exc)
            raise
        notes = ["no voice-activity gate: silent or noisy audio can make Whisper write words that were not spoken.",
                 "the language list comes from 60 read-speech FLEURS clips per language (coverage/stt.csv); "
                 "other audio can behave differently.",
                 "device requested: %s (compute type %s)." % (device, _DEVICES[device])]
        text = " ".join(p for p in parts if p)
        if not text:
            notes.append("the engine produced no text.")
        return Result(text=text, language=request.language, segments=tuple(segs), notes=tuple(notes))

    def close(self):
        self._model = None
        self._device = None
        self._path = None
