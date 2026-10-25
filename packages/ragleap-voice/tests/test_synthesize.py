import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from ragleap_voice import (Capabilities, EngineError, EngineNotInstalledError, ModelNotVerifiedError,  # noqa: E402
                           NoEngineError, Registry, Result, UnsupportedFeatureError, UnsupportedLanguageError,
                           synthesize)
from ragleap_voice.cache import model_dir  # noqa: E402
from ragleap_voice.synth import _synthesize  # noqa: E402
from test_transcribe import REV, make_env, wav_bytes  # noqa: E402,F401

_DEFAULT = object()


class FakeTts:
    def __init__(self, name="faketts", entries=("fake-model",), languages=("hi", "en"), kind="tts", emotion=(),
                 emotion_languages=(), watermarks=False, error=None, wav=_DEFAULT, claimed_rate=None):
        self.name = name
        self.manifest_entries = tuple(entries)
        self.capabilities = Capabilities(kind=kind, languages=frozenset(languages), emotion=frozenset(emotion),
                                         emotion_languages=frozenset(emotion_languages), watermarks_output=watermarks)
        self.error, self.wav, self.claimed_rate = error, wav, claimed_rate
        self.loaded, self.closed, self.request = None, False, None

    def load(self, model_dirs):
        self.loaded = dict(model_dirs)

    def run(self, request):
        self.request = request
        if self.error:
            raise self.error
        if self.wav is None:
            return Result(text="no audio here")
        wav = wav_bytes() if self.wav is _DEFAULT else self.wav
        return Result(wav=wav, sample_rate=self.claimed_rate, notes=("engine note",))

    def close(self):
        self.closed = True


def go(env, *engines, third=(), text="namaste", **kw):
    reg = Registry()
    for e in engines:
        reg.register(e)
    for e in third:
        reg.register(e, third_party=True)
    mpath, cache = env
    kw.setdefault("language", "hi")
    return _synthesize(text, reg, mpath, cache_dir=cache, **kw)


def test_happy_path(tmp_path):
    env = make_env(tmp_path)
    eng = FakeTts()
    s = go(env, eng, engine="faketts", speed=1.5, options={"x": 1})
    assert (s.engine, s.model, s.model_status, s.voice) == ("faketts", "fake-model", "verified", None)
    assert s.synthetic is True and s.watermarked is False
    assert s.sample_rate == 16000 and abs(s.duration_s - 0.1) < 1e-9 and s.wav[:4] == b"RIFF"
    assert "engine note" in s.notes
    assert eng.loaded == {"fake-model": model_dir("fake-model", REV, env[1])} and eng.closed
    assert (eng.request.kind, eng.request.text, eng.request.language, eng.request.speed) == ("tts", "namaste", "hi", 1.5)
    assert eng.request.options == {"x": 1}


def test_watermark_follows_engine(tmp_path):
    assert go(make_env(tmp_path), FakeTts(watermarks=True)).watermarked is True


def test_auto_choice(tmp_path):
    env = make_env(tmp_path)
    assert go(env, FakeTts(name="zzz"), third=(FakeTts(name="aaa"),)).engine == "zzz"
    with pytest.raises(UnsupportedLanguageError):
        go(env, FakeTts(languages=("en",)), language="ta")
    with pytest.raises(NoEngineError):
        go(env)


def test_named_engine_checks(tmp_path):
    env = make_env(tmp_path)
    with pytest.raises(EngineNotInstalledError):
        go(env, FakeTts(), engine="nope")
    with pytest.raises(UnsupportedFeatureError):
        go(env, FakeTts(kind="stt"), engine="faketts")
    with pytest.raises(UnsupportedLanguageError):
        go(env, FakeTts(), engine="faketts", language="ta")


def test_voice_is_reserved(tmp_path):
    eng = FakeTts()
    with pytest.raises(UnsupportedFeatureError) as info:
        go(make_env(tmp_path), eng, voice="someone")
    assert "763" in str(info.value) and eng.loaded is None


def test_emotion_rules(tmp_path):
    env = make_env(tmp_path)
    plain = FakeTts()
    with pytest.raises(UnsupportedFeatureError):
        go(env, plain, emotion="happy")
    assert plain.loaded is None
    with pytest.raises(UnsupportedFeatureError):
        go(env, FakeTts(emotion=("happy",), emotion_languages=("en",)), emotion="angry")
    eng = FakeTts(emotion=("happy",), emotion_languages=("en",))
    weak = go(env, eng, language="hi", emotion="happy")
    assert any("may be weak" in n for n in weak.notes) and eng.request.emotion == "happy"
    strong = go(env, FakeTts(emotion=("happy",), emotion_languages=("en",)), language="en", emotion="happy")
    assert not any("may be weak" in n for n in strong.notes)


def test_argument_checks(tmp_path):
    env = make_env(tmp_path)
    for kw, exc in (({"text": ""}, ValueError), ({"text": "   "}, ValueError), ({"text": 5}, TypeError),
                    ({"language": " "}, ValueError), ({"speed": 0}, ValueError), ({"speed": -1}, ValueError),
                    ({"speed": float("nan")}, ValueError), ({"speed": "fast"}, TypeError), ({"speed": True}, TypeError),
                    ({"emotion": " "}, ValueError), ({"options": [1]}, TypeError)):
        with pytest.raises(exc):
            go(env, FakeTts(), **kw)


def test_unverified_needs_flag(tmp_path):
    env = make_env(tmp_path, status="unverified")
    with pytest.raises(ModelNotVerifiedError):
        go(env, FakeTts())
    s = go(env, FakeTts(), allow_unverified=True)
    assert s.model_status == "unverified" and any("allow_unverified" in n for n in s.notes)


def test_engine_crash_becomes_engine_error(tmp_path):
    eng = FakeTts(error=RuntimeError("boom"))
    with pytest.raises(EngineError) as info:
        go(make_env(tmp_path), eng)
    assert isinstance(info.value.__cause__, RuntimeError) and eng.closed


def test_library_errors_pass_through(tmp_path):
    eng = FakeTts(error=UnsupportedFeatureError("no such option"))
    with pytest.raises(UnsupportedFeatureError):
        go(make_env(tmp_path), eng)
    assert eng.closed


def test_bad_engine_output(tmp_path):
    env = make_env(tmp_path)
    for bad in (FakeTts(wav=None), FakeTts(wav=b"not a wav"), FakeTts(wav=b""), FakeTts(wav=wav_bytes(frames=0)),
                FakeTts(claimed_rate=8000)):
        with pytest.raises(EngineError):
            go(env, bad)


def test_public_function_without_tts_engine():
    with pytest.raises(NoEngineError):
        synthesize("hello", language="en")
