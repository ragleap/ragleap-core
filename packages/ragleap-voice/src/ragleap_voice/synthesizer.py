"""Synthesizer: an opt-in object that keeps the ZONOS3-Mini server running between calls (docs/design/zonos3-engine.md).

Status: pre-alpha, not exported. synthesize() is unchanged: it opens and closes its engine on every call, so a call that uses the
ZONOS3-Mini engine starts and stops the server each time (about 15 s for a 2 s clip on one laptop CPU, one run). A Synthesizer owns
its own ZONOS3-Mini engine (never the registry's shared one), serialises calls with one lock and keeps the server until close().
Validation, model verification and the result are the same as in synthesize(). Hindi and Telugu only, stock voices only.
"""
import sys
import threading

from .engine import Capabilities
from .errors import RagleapVoiceError, UnsupportedFeatureError, UnsupportedLanguageError
from .registry import Registry
from .synth import _synthesize
from .zonos3_engine import LANGUAGES, MODEL, SCRIPT, Zonos3Engine

# These fail before the engine is touched, so the running server stays usable after them.
_KEEP = (UnsupportedFeatureError, UnsupportedLanguageError, ValueError, TypeError)


class _ResidentZonos3(Zonos3Engine):
    _path = None

    def load(self, model_dirs):
        path = model_dirs.get(MODEL)
        if self._worker is not None and path is not None and str(path) == self._path:
            return  # same verified folder: keep the running server
        if self._worker is not None:
            Zonos3Engine.close(self)  # another folder: stop the old server first
        super().load(model_dirs)
        self._path = None if path is None else str(path)

    def close(self):
        pass  # _synthesize() calls this after every call; the server stays up

    def release(self):
        Zonos3Engine.close(self)
        self._path = None


class Synthesizer:
    """synthesize() for the ZONOS3-Mini engine with the server kept running. Use as a context manager or call close().

    Extra keyword arguments (interpreter, load_timeout, run_timeout, base_dir) go to the engine. The server binary must be on
    the PATH of the process, as for the engine itself.
    """

    def __init__(self, *, manifest=None, **engine_options):
        caps = Capabilities(kind="tts", languages=LANGUAGES, hardware=frozenset({"cpu"}), isolated=True)
        engine_options.setdefault("interpreter", sys.executable)
        self._engine = _ResidentZonos3("zonos3", (MODEL,), caps, str(SCRIPT), **engine_options)
        self._registry = Registry()
        self._registry.register(self._engine)
        self._manifest = manifest  # a models.toml path override; None means the packaged one
        self._lock = threading.Lock()
        self._closed = False

    def synthesize(self, text, *, language, allow_unverified=False, cache_dir=None, options=None):
        """Same arguments and result as synthesize(), minus engine (always zonos3) and the reserved ones. Calls are serialised."""
        with self._lock:
            if self._closed:
                raise RagleapVoiceError("this Synthesizer is closed.", hint="create a new Synthesizer.")
            try:
                return _synthesize(text, self._registry, self._manifest, language=language, engine="zonos3",
                                   allow_unverified=allow_unverified, cache_dir=cache_dir, options=options)
            except _KEEP:
                raise
            except Exception:
                self._engine.release()  # unknown state: start the server again on the next call
                raise

    def close(self):
        """Wait for a call in progress, stop the server, and reject later calls. Idempotent."""
        with self._lock:
            self._closed = True
            self._engine.release()

    def __enter__(self):
        if self._closed:
            raise RagleapVoiceError("this Synthesizer is closed.", hint="create a new Synthesizer.")
        return self

    def __exit__(self, *exc):
        self.close()
        return False
