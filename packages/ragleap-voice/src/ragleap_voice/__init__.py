"""ragleap-voice: open, local speech-to-text and text-to-speech (pre-alpha)."""

from .engine import Capabilities, Engine, EngineInfo, Request, Result
from .errors import (AudioDecodeError, ConsentRequiredError, EngineError, EngineNotInstalledError,
                     EngineUnavailableError, LicenceError, ModelNotFoundError, ModelNotVerifiedError,
                     NoEngineError, RagleapVoiceError, UnsupportedFeatureError, UnsupportedLanguageError)
from .registry import Registry, engines, register_engine
from .results import Segment, Speech, Transcript
from .api import transcribe
from .gate import GateDecision
from .synth import synthesize
from .stream import transcribe_stream
from .synth_stream import synthesize_stream
from .whisper_engine import WhisperEngine
from .parler_engine import make_parler_engine

__version__ = "0.3.0"

register_engine(WhisperEngine())
register_engine(make_parler_engine())

__all__ = ["synthesize_stream", "transcribe_stream", "AudioDecodeError", "Capabilities", "ConsentRequiredError", "Engine", "EngineError", "EngineInfo",
           "EngineNotInstalledError", "EngineUnavailableError", "LicenceError", "ModelNotFoundError",
           "ModelNotVerifiedError", "NoEngineError", "RagleapVoiceError", "Registry", "Request", "Result",
           "Segment", "Speech", "Transcript", "UnsupportedFeatureError", "UnsupportedLanguageError",
           "engines", "register_engine", "synthesize", "transcribe", "GateDecision"]
