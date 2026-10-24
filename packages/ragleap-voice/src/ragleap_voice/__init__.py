"""ragleap-voice: open, local speech-to-text and text-to-speech (pre-alpha)."""

from .engine import Capabilities, Engine, EngineInfo, Request, Result
from .errors import (AudioDecodeError, ConsentRequiredError, EngineError, EngineNotInstalledError,
                     EngineUnavailableError, LicenceError, ModelNotFoundError, ModelNotVerifiedError,
                     NoEngineError, RagleapVoiceError, UnsupportedFeatureError, UnsupportedLanguageError)
from .registry import Registry, engines, register_engine
from .results import Segment, Speech, Transcript

__version__ = "0.0.2"

__all__ = ["AudioDecodeError", "Capabilities", "ConsentRequiredError", "Engine", "EngineError", "EngineInfo",
           "EngineNotInstalledError", "EngineUnavailableError", "LicenceError", "ModelNotFoundError",
           "ModelNotVerifiedError", "NoEngineError", "RagleapVoiceError", "Registry", "Request", "Result",
           "Segment", "Speech", "Transcript", "UnsupportedFeatureError", "UnsupportedLanguageError",
           "engines", "register_engine"]
