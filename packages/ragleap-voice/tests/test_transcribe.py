import hashlib
import io
import json
import pathlib
import sys
import wave

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from ragleap_voice import (AudioDecodeError, Capabilities, EngineError, EngineNotInstalledError, LicenceError,  # noqa: E402
                           ModelNotFoundError, ModelNotVerifiedError, NoEngineError, Registry, Result, Segment,
                           UnsupportedFeatureError, UnsupportedLanguageError)
from ragleap_voice.api import _transcribe  # noqa: E402
from ragleap_voice.cache import model_dir  # noqa: E402
from ragleap_voice.locks import lock_digest  # noqa: E402
from ragleap_voice.wavio import read_wav  # noqa: E402

REV = "a" * 40
TOML = """[[model]]
name = "%(name)s"
kind = "stt"
source_url = "https://huggingface.co/example/fake"
revision = "%(rev)s"
sha256 = "%(digest)s"
code_license = "%(lic)s"
weights_license = "%(lic)s"
languages = []
capabilities = []
min_hardware = "cpu"
status = "%(status)s"
default = false
"""


def wav_bytes(frames=1600, rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * frames)
    return buf.getvalue()


def make_env(tmp_path, status="verified", lic="MIT", pulled=True, tamper=False, name="fake-model"):
    content = b"abc"
    lock = {"schema": 1, "model": name, "revision": REV, "source_url": "https://huggingface.co/example/fake",
            "files": [{"path": "weights.txt", "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}]}
    (tmp_path / "locks").mkdir()
    (tmp_path / "locks" / (name + ".json")).write_text(json.dumps(lock))
    mpath = tmp_path / "models.toml"
    mpath.write_text(TOML % {"name": name, "rev": REV, "digest": lock_digest(lock), "lic": lic, "status": status})
    cache = tmp_path / "cache"
    if pulled:
        d = model_dir(name, REV, cache)
        d.mkdir(parents=True)
        (d / "weights.txt").write_bytes(b"abX" if tamper else content)
    return mpath, cache


class FakeStt:
    def __init__(self, name="fake", entries=("fake-model",), languages=("en", "hi"), detects=False, kind="stt",
                 error=None, text="hello"):
        self.name = name
        self.manifest_entries = tuple(entries)
        self.capabilities = Capabilities(kind=kind, languages=frozenset(languages), detects_language=detects)
        self.error = error
        self.text = text
        self.loaded = None
        self.closed = False
        self.request = None

    def load(self, model_dirs):
        self.loaded = dict(model_dirs)

    def run(self, request):
        self.request = request
        if self.error:
            raise self.error
        return Result(text=self.text, language=request.language or "en", segments=(Segment(0.0, 1.0, self.text),),
                      notes=("engine note",))

    def close(self):
        self.closed = True


def go(env, *engines, third=(), audio=None, **kw):
    reg = Registry()
    for e in engines:
        reg.register(e)
    for e in third:
        reg.register(e, third_party=True)
    mpath, cache = env
    return _transcribe(audio if audio is not None else wav_bytes(), reg, mpath, cache_dir=cache, **kw)


def test_capability_default():
    assert Capabilities(kind="stt").detects_language is False


def test_happy_path(tmp_path):
    env = make_env(tmp_path)
    eng = FakeStt()
    t = go(env, eng, language="hi", engine="fake")
    assert (t.text, t.engine, t.model, t.model_status, t.language) == ("hello", "fake", "fake-model", "verified", "hi")
    assert eng.loaded == {"fake-model": model_dir("fake-model", REV, env[1])}
    assert eng.closed and t.segments == () and "engine note" in t.notes
    assert eng.request.kind == "stt" and eng.request.audio[:4] == b"RIFF"


def test_timestamps_keep_segments(tmp_path):
    t = go(make_env(tmp_path), FakeStt(), language="en", engine="fake", timestamps=True)
    assert t.segments == (Segment(0.0, 1.0, "hello"),)


def test_auto_picks_declared_language_and_skips_third_party(tmp_path):
    env = make_env(tmp_path)
    t = go(env, FakeStt(name="zzz"), third=(FakeStt(name="aaa"),), language="en")
    assert t.engine == "zzz"


def test_auto_unsupported_language(tmp_path):
    with pytest.raises(UnsupportedLanguageError):
        go(make_env(tmp_path), FakeStt(languages=("en",)), language="ta")


def test_auto_without_any_stt_engine(tmp_path):
    with pytest.raises(NoEngineError):
        go(make_env(tmp_path), language="en")


def test_auto_language_none_needs_detection(tmp_path):
    env = make_env(tmp_path)
    with pytest.raises(NoEngineError):
        go(env, FakeStt(detects=False))
    t = go(env, FakeStt(detects=True))
    assert t.language == "en"


def test_named_engine_checks(tmp_path):
    env = make_env(tmp_path)
    with pytest.raises(EngineNotInstalledError):
        go(env, FakeStt(), language="en", engine="nope")
    with pytest.raises(UnsupportedFeatureError):
        go(env, FakeStt(kind="tts"), language="en", engine="fake")
    with pytest.raises(UnsupportedLanguageError):
        go(env, FakeStt(), language="ta", engine="fake")
    with pytest.raises(UnsupportedFeatureError):
        go(env, FakeStt(), engine="fake")


def test_unverified_needs_flag(tmp_path):
    env = make_env(tmp_path, status="unverified")
    with pytest.raises(ModelNotVerifiedError) as info:
        go(env, FakeStt(), language="en", engine="fake")
    assert "Next:" in str(info.value)
    t = go(env, FakeStt(), language="en", engine="fake", allow_unverified=True)
    assert t.model_status == "unverified" and any("allow_unverified" in n for n in t.notes)


def test_licence_fails(tmp_path):
    with pytest.raises(LicenceError):
        go(make_env(tmp_path, lic="GPL-3.0-only"), FakeStt(), language="en", engine="fake", allow_unverified=True)


def test_model_not_pulled(tmp_path):
    with pytest.raises(ModelNotFoundError):
        go(make_env(tmp_path, pulled=False), FakeStt(), language="en", engine="fake")


def test_tampered_model_refused(tmp_path):
    eng = FakeStt()
    with pytest.raises(ModelNotVerifiedError):
        go(make_env(tmp_path, tamper=True), eng, language="en", engine="fake")
    assert eng.loaded is None


def test_unknown_manifest_entry(tmp_path):
    with pytest.raises(ModelNotFoundError):
        go(make_env(tmp_path), FakeStt(entries=("other",)), language="en", engine="fake")


def test_engine_crash_becomes_engine_error_and_closes(tmp_path):
    eng = FakeStt(error=RuntimeError("boom"))
    with pytest.raises(EngineError) as info:
        go(make_env(tmp_path), eng, language="en", engine="fake")
    assert isinstance(info.value.__cause__, RuntimeError) and eng.closed


def test_library_errors_pass_through(tmp_path):
    eng = FakeStt(error=UnsupportedFeatureError("no such option"))
    with pytest.raises(UnsupportedFeatureError):
        go(make_env(tmp_path), eng, language="en", engine="fake", options={"x": 1})
    assert eng.closed and eng.request.options == {"x": 1}


def test_bad_audio(tmp_path):
    env = make_env(tmp_path)
    for bad in (b"not a wav file", wav_bytes(frames=0), b""):
        with pytest.raises(AudioDecodeError):
            go(env, FakeStt(), audio=bad, language="en", engine="fake")
    with pytest.raises(AudioDecodeError):
        read_wav(str(tmp_path / "missing.wav"))
    with pytest.raises(TypeError):
        read_wav(12)


def test_wav_from_path(tmp_path):
    p = tmp_path / "a.wav"
    p.write_bytes(wav_bytes(frames=8000, rate=8000))
    data, info = read_wav(str(p))
    assert data[:4] == b"RIFF" and info.sample_rate == 8000 and info.duration_s == 1.0


def test_argument_checks(tmp_path):
    env = make_env(tmp_path)
    with pytest.raises(ValueError):
        go(env, FakeStt(), language="  ")
    with pytest.raises(TypeError):
        go(env, FakeStt(), language="en", engine="fake", options=[1])
