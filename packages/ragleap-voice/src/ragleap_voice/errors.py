"""Exception hierarchy for ragleap-voice (planned API, see docs/design/api.md section 9). Standard library only.

Every error says what happened and, through the optional hint, what to do next. No error is swallowed and turned into a default.
"""


class RagleapVoiceError(Exception):
    """Base class for every error this package raises on purpose."""

    def __init__(self, message, *, hint=None):
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self):
        return self.message if not self.hint else "%s Next: %s" % (self.message.rstrip(), self.hint)


class ModelNotFoundError(RagleapVoiceError):
    """The model has not been pulled into the cache."""


class ModelNotVerifiedError(RagleapVoiceError):
    """The model is unverified and allow_unverified was not given."""


class LicenceError(RagleapVoiceError):
    """A manifest entry fails the licence allowlist."""


class EngineNotInstalledError(RagleapVoiceError):
    """The engine, or the optional extra that provides it, is not installed."""


class EngineUnavailableError(RagleapVoiceError):
    """The engine is installed but cannot run here (for example CUDA is needed and not present)."""


class NoEngineError(RagleapVoiceError):
    """engine="auto" found no eligible engine."""


class UnsupportedLanguageError(RagleapVoiceError):
    """The engine does not support the requested language."""


class UnsupportedFeatureError(RagleapVoiceError):
    """The engine does not have the requested feature (for example emotion)."""


class AudioDecodeError(RagleapVoiceError):
    """The audio could not be decoded."""


class ConsentRequiredError(RagleapVoiceError):
    """Reserved for voice cloning: a consent record is required (design issue #763)."""


class EngineError(RagleapVoiceError):
    """An engine or its worker process failed. log_path says where its log is, when there is one."""

    def __init__(self, message, *, hint=None, log_path=None):
        super().__init__(message, hint=hint)
        self.log_path = log_path
