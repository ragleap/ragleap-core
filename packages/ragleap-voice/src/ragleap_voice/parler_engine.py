"""Indic Parler-TTS as an isolated worker engine (docs/design/api.md sections 4, 5 and 8). Standard library only.

The engine runs parler_worker.py in the Python environment named by RAGLEAP_VOICE_PARLER_PYTHON. The core never installs
that environment. Both model folders (the model and the locked flan-t5 tokenizer) are verified before the worker sees them.
"""
import pathlib

from .engine import Capabilities
from .errors import UnsupportedFeatureError
from .worker import WorkerEngine

MODEL, TOKENIZER = "indic-parler-tts", "flan-t5-large-tokenizer"
LANGUAGES = frozenset({"hi", "ta", "te", "ml", "kn", "en"})
INTERPRETER_ENV = "RAGLEAP_VOICE_PARLER_PYTHON"
SCRIPT = pathlib.Path(__file__).with_name("parler_worker.py")


class ParlerEngine(WorkerEngine):
    def run(self, request):
        if request.speed is not None:
            raise UnsupportedFeatureError("Parler has no speed control.",
                                          hint='describe the pace in options={"description": "..."}.')
        if request.emotion is not None:
            raise UnsupportedFeatureError("Parler declares no named emotion controls yet.",
                                          hint='describe the style in options={"description": "..."}.')
        if request.voice is not None:
            raise UnsupportedFeatureError("the voice parameter is reserved (cloning needs consent, issue #763).")
        return super().run(request)


def make_parler_engine(**kw):
    caps = Capabilities(kind="tts", languages=LANGUAGES, hardware=frozenset({"cpu", "cuda"}), isolated=True)
    kw.setdefault("interpreter_env", INTERPRETER_ENV)
    return ParlerEngine("parler", (MODEL, TOKENIZER), caps, str(SCRIPT), **kw)
