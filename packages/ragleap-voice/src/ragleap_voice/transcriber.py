"""Transcriber: an opt-in object that keeps the Whisper model open between calls (docs/design/streaming.md).

Status: pre-alpha. transcribe() is unchanged: it still opens the model on every call. A Transcriber owns its own WhisperEngine
(never the registry's shared one), serialises calls with one lock, and keeps the opened model until close(). Model verification
still goes through the same path as transcribe(), including the once-per-process cache. Not measured: the expected saving is the
per-call model open (about 2.7 s on one Colab T4, inferred), not a latency claim.
"""
import threading

from .api import _transcribe
from .errors import (AudioDecodeError, EngineError, RagleapVoiceError, UnsupportedFeatureError,
                     UnsupportedLanguageError)
from .registry import Registry
from .whisper_engine import MODEL, WhisperEngine

# These fail before the model is touched, so the open model stays usable after them.
_KEEP = (UnsupportedFeatureError, UnsupportedLanguageError, AudioDecodeError, ValueError, TypeError)


class _ResidentWhisper(WhisperEngine):
    def load(self, model_dirs):
        path = model_dirs.get(MODEL)
        if self._model is not None and path is not None and str(path) == self._path:
            return  # same verified folder: keep the open model
        super().load(model_dirs)

    def close(self):
        pass  # _transcribe() calls this after every call; the model stays open

    def release(self):
        self._model = None
        self._device = None
        self._path = None


class Transcriber:
    """transcribe() for Whisper with the model kept open. Use as a context manager or call close()."""

    def __init__(self, *, manifest=None):
        self._engine = _ResidentWhisper()
        self._registry = Registry()
        self._registry.register(self._engine)
        self._manifest = manifest  # a models.toml path override, as in the CLI; None means the packaged one
        self._lock = threading.Lock()
        self._closed = False

    def transcribe(self, audio, *, language=None, allow_unverified=False, cache_dir=None, timestamps=False,
                   options=None, gate="off", gate_options=None):
        """Same arguments and result as transcribe(), minus engine (always whisper). Calls are serialised."""
        with self._lock:
            if self._closed:
                raise RagleapVoiceError("this Transcriber is closed.", hint="create a new Transcriber.")
            try:
                return _transcribe(audio, self._registry, self._manifest, language=language, engine="whisper",
                                   allow_unverified=allow_unverified, cache_dir=cache_dir, timestamps=timestamps,
                                   options=options, gate=gate, gate_options=gate_options)
            except _KEEP:
                raise
            except Exception:
                self._engine.release()  # unknown state: reopen on the next call
                raise

    def close(self):
        """Wait for a call in progress, free the model, and reject later calls. Idempotent."""
        with self._lock:
            self._closed = True
            self._engine.release()

    def __enter__(self):
        if self._closed:
            raise RagleapVoiceError("this Transcriber is closed.", hint="create a new Transcriber.")
        return self

    def __exit__(self, *exc):
        self.close()
        return False
