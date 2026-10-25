"""synthesize(), text to speech (docs/design/api.md sections 2, 6, 7, 9 and 10). Standard library only.

Same rules as transcribe(): the engine gets verified model directories and never fetches anything; no error is swallowed
and turned into a default. The voice parameter is reserved until the consent store exists (issue #763).
"""
import io
import math
import wave
from collections.abc import Mapping

from .api import _resolve_models
from .engine import Request, Result
from .errors import (EngineError, NoEngineError, RagleapVoiceError, UnsupportedFeatureError,
                     UnsupportedLanguageError)
from .registry import _DEFAULT as _DEFAULT_REGISTRY
from .results import Speech


def _pick_engine(registry, name, language):
    if name != "auto":
        eng = registry.get(name)
        caps = eng.capabilities
        if caps.kind != "tts":
            raise UnsupportedFeatureError("engine %r is a %s engine, not a text-to-speech engine." % (name, caps.kind),
                                          hint="choose an engine from engines('tts').")
        if language not in caps.languages:
            raise UnsupportedLanguageError("engine %r does not declare language %r." % (name, language),
                                           hint="see engines('tts') for the languages each engine declares.")
        return eng
    candidates = registry.auto_candidates("tts", language)
    if candidates:
        return candidates[0]
    if not [i for i in registry.info("tts") if not i.third_party]:
        raise NoEngineError("no built-in text-to-speech engine is registered.",
                            hint="engines arrive as optional extras; install the one you need.")
    raise UnsupportedLanguageError("no built-in text-to-speech engine declares language %r." % language,
                                   hint="see engines('tts'); you can also name an engine explicitly.")


def _wav_facts(name, data):
    """(sample_rate, duration_s) read from the WAV header of an engine's output, or EngineError."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise EngineError("engine %r returned no audio." % name, hint="this is a bug in the engine.")
    try:
        with wave.open(io.BytesIO(bytes(data)), "rb") as w:
            rate, frames = w.getframerate(), w.getnframes()
    except (wave.Error, EOFError) as exc:
        raise EngineError("engine %r returned audio that is not a readable PCM WAV (%s)." % (name, exc),
                          hint="this is a bug in the engine.")
    if rate <= 0 or frames == 0:
        raise EngineError("engine %r returned a WAV without audio frames." % name, hint="this is a bug in the engine.")
    return rate, frames / float(rate)


def _synthesize(text, registry, manifest_path, *, language, engine="auto", voice=None, emotion=None, speed=None,
                allow_unverified=False, cache_dir=None, options=None):
    if not isinstance(text, str):
        raise TypeError("text must be a str")
    if not text.strip():
        raise ValueError("text must not be empty")
    if not isinstance(language, str) or not language.strip():
        raise ValueError("language is required, a code such as 'hi', 'ta', 'en'")
    if not isinstance(engine, str) or not engine:
        raise ValueError("engine must be 'auto' or an engine name")
    if options is not None and not isinstance(options, Mapping):
        raise TypeError("options must be a dict or None")
    if speed is not None:
        if isinstance(speed, bool) or not isinstance(speed, (int, float)):
            raise TypeError("speed must be a number or None")
        if not math.isfinite(speed) or speed <= 0:
            raise ValueError("speed must be a finite number above 0")
    if emotion is not None and (not isinstance(emotion, str) or not emotion.strip()):
        raise ValueError("emotion must be None or a non-empty name")
    if voice is not None:
        raise UnsupportedFeatureError("the voice parameter is reserved and not available in this version.",
                                      hint="voice cloning needs a consent record that does not exist yet (issue #763).")
    eng = _pick_engine(registry, engine, language)
    caps = eng.capabilities
    notes = []
    if emotion is not None:
        if not caps.emotion:
            raise UnsupportedFeatureError("engine %r has no emotion control." % eng.name,
                                          hint="leave emotion out, or choose an engine from engines('tts') that declares one.")
        if emotion not in caps.emotion:
            raise UnsupportedFeatureError("engine %r does not declare the emotion %r." % (eng.name, emotion),
                                          hint="declared emotions: %s." % ", ".join(sorted(caps.emotion)))
        if language not in caps.emotion_languages:
            notes.append("emotion %r is not officially supported for language %r by the model's authors; the effect may be weak."
                         % (emotion, language))
    dirs, status, unverified = _resolve_models(eng, manifest_path, cache_dir, allow_unverified)
    request = Request(kind="tts", language=language, text=text, emotion=emotion,
                      speed=None if speed is None else float(speed), options=dict(options or {}))
    closing = None
    try:
        try:
            eng.load(dirs)
            res = eng.run(request)
        finally:
            try:
                eng.close()
            except Exception as exc:
                closing = type(exc).__name__
    except RagleapVoiceError:
        raise
    except Exception as exc:
        raise EngineError("engine %r failed: %s: %s" % (eng.name, type(exc).__name__, exc),
                          hint="the original error is chained; run the engine on its own to investigate.") from exc
    if not isinstance(res, Result) or res.wav is None:
        raise EngineError("engine %r returned no audio result." % eng.name, hint="this is a bug in the engine.")
    rate, duration = _wav_facts(eng.name, res.wav)
    if res.sample_rate is not None and res.sample_rate != rate:
        raise EngineError("engine %r claims %r Hz but its WAV says %d Hz." % (eng.name, res.sample_rate, rate),
                          hint="this is a bug in the engine.")
    notes = list(res.notes) + notes
    if unverified:
        notes.append("unverified model(s) used because allow_unverified=True: %s." % ", ".join(unverified))
    if closing:
        notes.append("engine close() raised %s." % closing)
    return Speech(wav=bytes(res.wav), sample_rate=rate, duration_s=duration, engine=eng.name,
                  model=", ".join(eng.manifest_entries), model_status=status, voice=None, synthetic=True,
                  watermarked=bool(caps.watermarks_output), notes=tuple(notes))


def synthesize(text, *, language, engine="auto", voice=None, emotion=None, speed=None, allow_unverified=False,
               cache_dir=None, options=None):
    """Text to speech. Returns a Speech, always marked synthetic. voice is reserved (cloning needs consent, issue #763)."""
    return _synthesize(text, _DEFAULT_REGISTRY, None, language=language, engine=engine, voice=voice, emotion=emotion,
                       speed=speed, allow_unverified=allow_unverified, cache_dir=cache_dir, options=options)
