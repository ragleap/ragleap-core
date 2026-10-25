import os
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import ragleap_voice  # noqa: E402
from ragleap_voice import (EngineError, EngineNotInstalledError, ModelNotVerifiedError, Registry, Request,  # noqa: E402
                           Segment, UnsupportedFeatureError, engines)
from ragleap_voice.api import _transcribe  # noqa: E402
from ragleap_voice.whisper_engine import LANGUAGES, MODEL, WhisperEngine  # noqa: E402
from test_transcribe import make_env, wav_bytes  # noqa: E402,F401


class FakeModel:
    instances = []

    def __init__(self, path, **kw):
        self.path, self.kw, self.calls = path, kw, []
        self.env = (os.environ.get("HF_HUB_OFFLINE"), os.environ.get("TRANSFORMERS_OFFLINE"))
        FakeModel.instances.append(self)

    def transcribe(self, audio, **kw):
        self.calls.append((audio.read(4), kw))
        segs = [types.SimpleNamespace(start=0.0, end=1.5, text=" hello "),
                types.SimpleNamespace(start=1.5, end=2.0, text=" world")]
        return iter(segs), types.SimpleNamespace(language=kw.get("language"))


@pytest.fixture
def fake_fw(monkeypatch):
    FakeModel.instances = []
    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=FakeModel))


def loaded(tmp_path):
    eng = WhisperEngine()
    eng.load({MODEL: tmp_path})
    return eng


def req(**kw):
    return Request(kind="stt", language="en", audio=wav_bytes(), **kw)


def test_missing_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    with pytest.raises(EngineNotInstalledError) as info:
        WhisperEngine().load({MODEL: pathlib.Path("x")})
    assert 'ragleap-voice[whisper]' in str(info.value)


def test_load_is_local_and_offline(fake_fw, tmp_path, monkeypatch):
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)
    loaded(tmp_path)
    m = FakeModel.instances[0]
    assert m.path == str(tmp_path) and m.kw["local_files_only"] is True
    assert m.env == ("1", "1")
    assert "HF_HUB_OFFLINE" not in os.environ and "TRANSFORMERS_OFFLINE" not in os.environ


def test_load_restores_existing_env(fake_fw, tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    loaded(tmp_path)
    assert os.environ["HF_HUB_OFFLINE"] == "0"


def test_load_without_folder(fake_fw):
    with pytest.raises(EngineError):
        WhisperEngine().load({})


def test_run_text_and_segments(fake_fw, tmp_path):
    eng = loaded(tmp_path)
    r = eng.run(req(timestamps=True))
    assert r.text == "hello world" and r.language == "en"
    assert r.segments == (Segment(0.0, 1.5, "hello"), Segment(1.5, 2.0, "world"))
    magic, kw = FakeModel.instances[0].calls[0]
    assert magic == b"RIFF" and kw["vad_filter"] is False and kw["beam_size"] == 5 and kw["language"] == "en"
    assert any("voice-activity" in n for n in r.notes)
    assert eng.run(req()).segments == ()


def test_options(fake_fw, tmp_path):
    eng = loaded(tmp_path)
    eng.run(req(options={"beam_size": 2}))
    assert FakeModel.instances[0].calls[0][1]["beam_size"] == 2
    for bad in ({"pitch": 1}, {"beam_size": 0}, {"beam_size": "5"}, {"beam_size": True}):
        with pytest.raises(UnsupportedFeatureError):
            eng.run(req(options=bad))


def test_run_before_load_and_after_close(fake_fw, tmp_path):
    with pytest.raises(EngineError):
        WhisperEngine().run(req())
    eng = loaded(tmp_path)
    eng.close()
    with pytest.raises(EngineError):
        eng.run(req())


def test_registered_with_measured_languages():
    info = [i for i in engines("stt") if i.name == "whisper"]
    assert len(info) == 1 and not info[0].third_party
    assert info[0].capabilities.languages == LANGUAGES == {"es", "fr", "pt", "ru", "tr", "id", "vi", "en", "ja"}
    assert info[0].capabilities.detects_language is False
    assert info[0].manifest_entries == (MODEL,)


def test_through_transcribe(fake_fw, tmp_path):
    env = make_env(tmp_path, status="unverified", name=MODEL)
    reg = Registry()
    reg.register(WhisperEngine())
    with pytest.raises(ModelNotVerifiedError):
        _transcribe(wav_bytes(), reg, env[0], cache_dir=env[1], language="en")
    t = _transcribe(wav_bytes(), reg, env[0], cache_dir=env[1], language="en", allow_unverified=True)
    assert t.text == "hello world" and t.engine == "whisper" and t.model_status == "unverified"
    assert any("allow_unverified" in n for n in t.notes)
