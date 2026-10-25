"""Whisper large-v3 speech-to-text engine on faster-whisper (CTranslate2). docs/design/api.md sections 4, 5 and 7.

The core stays standard library only: faster_whisper is imported when the engine loads. The engine is handed a verified
directory and never fetches anything; it is told to use local files only.
"""
import contextlib
import io
import os

from .engine import Capabilities, Result
from .errors import EngineError, EngineNotInstalledError, UnsupportedFeatureError
from .results import Segment

MODEL = "whisper-large-v3"
# Languages with a usable measured result in coverage/stt.csv (60 read-speech FLEURS clips each, en only 20).
LANGUAGES = frozenset({"es", "fr", "pt", "ru", "tr", "id", "vi", "en", "ja"})
_OFFLINE = {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
_INSTALL = 'pip install "ragleap-voice[whisper]"'
_OPTIONS = ("beam_size",)


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


class WhisperEngine:
    name = "whisper"
    manifest_entries = (MODEL,)
    capabilities = Capabilities(kind="stt", languages=LANGUAGES, hardware=frozenset({"cpu", "cuda"}))

    def __init__(self):
        self._model = None

    def load(self, model_dirs):
        try:
            import faster_whisper
        except ImportError:
            raise EngineNotInstalledError("the faster-whisper package is not installed.",
                                          hint="install the extra: " + _INSTALL)
        path = model_dirs.get(MODEL)
        if path is None:
            raise EngineError("the verified folder of %r was not provided." % MODEL)
        with _offline_env():
            self._model = faster_whisper.WhisperModel(str(path), device="auto", compute_type="default",
                                                      local_files_only=True)

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
        segments, _info = self._model.transcribe(io.BytesIO(request.audio), language=request.language,
                                                 beam_size=beam, vad_filter=False)
        parts, segs = [], []
        for s in segments:
            text = s.text.strip()
            parts.append(text)
            if request.timestamps:
                segs.append(Segment(float(s.start), float(s.end), text))
        notes = ["no voice-activity gate: silent or noisy audio can make Whisper write words that were not spoken.",
                 "the language list comes from 60 read-speech FLEURS clips per language (coverage/stt.csv); "
                 "other audio can behave differently."]
        text = " ".join(p for p in parts if p)
        if not text:
            notes.append("the engine produced no text.")
        return Result(text=text, language=request.language, segments=tuple(segs), notes=tuple(notes))

    def close(self):
        self._model = None
