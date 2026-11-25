"""ZONOS3-Mini as an isolated worker engine (docs/design/zonos3-engine.md). Standard library only.

The engine runs zonos3_worker.py, which starts the operator-built zonos3-server from the verified model folder. The server
binary must be on the PATH of the process that starts the engine; it is not covered by the lock. Not registered by importing
this module and not exported: build it with make_zonos3_engine() and register it yourself. Hindi and Telugu only, stock voices
only, no cloning (issue #763).
"""
import pathlib
import sys

from .engine import Capabilities
from .errors import UnsupportedFeatureError
from .worker import WorkerEngine

MODEL = "zonos3-mini"
LANGUAGES = frozenset({"hi", "te"})
SCRIPT = pathlib.Path(__file__).with_name("zonos3_worker.py")
# Checked here before the server is asked, so a typo does not restart it. The worker script checks the same things (a test compares them).
_OPTIONS = ("seed",)
MAX_CHARS = 2000  # a starting value, not measured


class Zonos3Engine(WorkerEngine):
    def run(self, request):
        if request.speed is not None:
            raise UnsupportedFeatureError("the ZONOS3-Mini engine has no speed control yet.")
        if request.emotion is not None:
            raise UnsupportedFeatureError("the ZONOS3-Mini engine declares no emotion controls yet.")
        if request.voice is not None:
            raise UnsupportedFeatureError("the voice parameter is reserved (cloning needs consent, issue #763).")
        if request.language not in LANGUAGES:
            raise UnsupportedFeatureError("the ZONOS3-Mini engine supports only hi and te.",
                                          hint="pass language='hi' or language='te'.")
        options = request.options or {}
        unknown = sorted(set(options) - set(_OPTIONS))
        if unknown:
            raise UnsupportedFeatureError("unsupported option(s): %s." % ", ".join(unknown), hint="supported options: %s." % ", ".join(_OPTIONS))
        seed = options.get("seed", 1)
        if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2 ** 31:
            raise UnsupportedFeatureError("seed must be an integer from 0 to 2147483647.")
        if request.text is not None and len(request.text) > MAX_CHARS:
            raise UnsupportedFeatureError("the text is longer than %d characters." % MAX_CHARS, hint="split it into shorter pieces.")
        return super().run(request)


def make_zonos3_engine(**kw):
    caps = Capabilities(kind="tts", languages=LANGUAGES, hardware=frozenset({"cpu"}), isolated=True)
    kw.setdefault("interpreter", sys.executable)
    return Zonos3Engine("zonos3", (MODEL,), caps, str(SCRIPT), **kw)
