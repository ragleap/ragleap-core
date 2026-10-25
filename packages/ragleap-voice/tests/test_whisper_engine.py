import io
import os
import pathlib
import sys
import types
import wave

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import ragleap_voice  # noqa: E402
from ragleap_voice import (AudioDecodeError, EngineError, EngineNotInstalledError, EngineUnavailableError,  # noqa: E402
                           ModelNotVerifiedError, Registry, Request, Segment, UnsupportedFeatureError, engines)
from ragleap_voice.api import _transcribe  # noqa: E402
from ragleap_voice.whisper_engine import LANGUAGES, MODEL, WhisperEngine, _samples  # noqa: E402
from test_transcribe import make_env, wav_bytes  # noqa: E402,F401


class FakeArr:
    def __init__(self, raw, dtype):
        self.raw, self.dtype, self.cast, self.divisor = raw, dtype, None, None

    def astype(self, t):
        self.cast = t
        return self

    def __truediv__(self, d):
        self.divisor = d
        return self


class FakeModel:
    instances = []
    fail = None

    def __init__(self, path, **kw):
        self.path, self.kw, self.calls = path, kw, []
        self.env = (os.environ.get("HF_HUB_OFFLINE"), os.environ.get("TRANSFORMERS_OFFLINE"))
        FakeModel.instances.append(self)

    def transcribe(self, audio, **kw):
        self.calls.append((audio, kw))

        def gen():
            if FakeModel.fail:
                raise FakeModel.fail
            yield types.SimpleNamespace(start=0.0, end=1.5, text=" hello ")
            yield types.SimpleNamespace(start=1.5, end=2.0, text=" world")
        return gen(), types.SimpleNamespace(language=kw.get("language"))


@pytest.fixture
def fake_fw(monkeypatch):
    FakeModel.instances, FakeModel.fail = [], None
    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=FakeModel))
    monkeypatch.setitem(sys.modules, "numpy", types.SimpleNamespace(frombuffer=lambda raw, dtype: FakeArr(raw, dtype)))


def wav_custom(rate=16000, channels=1, width=2, frames=160, data=None):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(data if data is not None else b"\x00" * width * channels * frames)
    return buf.getvalue()


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


def test_missing_numpy(monkeypatch):
    monkeypatch.setitem(sys.modules, "numpy", None)
    with pytest.raises(EngineNotInstalledError) as info:
        _samples(wav_bytes())
    assert "numpy" in str(info.value)


def test_load_is_local_and_offline(fake_fw, tmp_path, monkeypatch):
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)
    loaded(tmp_path)
    m = FakeModel.instances[0]
    assert m.path == str(tmp_path) and m.kw["local_files_only"] is True
    assert m.kw["device"] == "auto" and m.kw["compute_type"] == "default"
    assert m.env == ("1", "1")
    assert "HF_HUB_OFFLINE" not in os.environ and "TRANSFORMERS_OFFLINE" not in os.environ


def test_load_restores_existing_env(fake_fw, tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    loaded(tmp_path)
    assert os.environ["HF_HUB_OFFLINE"] == "0"


def test_load_without_folder(fake_fw):
    with pytest.raises(EngineError):
        WhisperEngine().load({})


def test_run_passes_samples_not_a_stream(fake_fw, tmp_path):
    eng = loaded(tmp_path)
    r = eng.run(req(timestamps=True))
    assert r.text == "hello world" and r.language == "en"
    assert r.segments == (Segment(0.0, 1.5, "hello"), Segment(1.5, 2.0, "world"))
    arr, kw = FakeModel.instances[0].calls[0]
    assert isinstance(arr, FakeArr) and len(arr.raw) == 3200 and arr.dtype == "<i2"
    assert arr.cast == "float32" and arr.divisor == 32768.0
    assert kw["vad_filter"] is False and kw["beam_size"] == 5 and kw["language"] == "en"
    assert any("voice-activity" in n for n in r.notes)
    assert eng.run(req()).segments == ()


def test_real_numpy_values():
    np = pytest.importorskip("numpy")
    data = np.array([16384, -16384, 0], dtype="<i2").tobytes()
    arr = _samples(wav_custom(frames=3, data=data))
    assert arr.dtype == np.float32 and arr.tolist() == [0.5, -0.5, 0.0]


def test_unsupported_audio_formats(fake_fw, tmp_path):
    eng = loaded(tmp_path)
    for bad in (wav_custom(rate=8000), wav_custom(channels=2), wav_custom(width=1)):
        with pytest.raises(UnsupportedFeatureError) as info:
            eng.run(Request(kind="stt", language="en", audio=bad))
        assert "ffmpeg" in str(info.value)
    with pytest.raises(AudioDecodeError):
        eng.run(Request(kind="stt", language="en", audio=b"not a wav"))
    assert FakeModel.instances[0].calls == []


def test_options(fake_fw, tmp_path):
    eng = loaded(tmp_path)
    eng.run(req(options={"beam_size": 2}))
    assert FakeModel.instances[0].calls[0][1]["beam_size"] == 2
    for bad in ({"pitch": 1}, {"beam_size": 0}, {"beam_size": "5"}, {"beam_size": True},
                {"device": "tpu"}, {"device": 1}, {"device": ["cpu"]}):
        with pytest.raises(UnsupportedFeatureError):
            eng.run(req(options=bad))


def test_device_option_reloads_only_when_it_changes(fake_fw, tmp_path):
    eng = loaded(tmp_path)
    eng.run(req(options={"device": "cpu"}))
    eng.run(req(options={"device": "cpu"}))
    assert len(FakeModel.instances) == 2
    first, second = FakeModel.instances
    assert (first.kw["device"], first.kw["compute_type"]) == ("auto", "default")
    assert (second.kw["device"], second.kw["compute_type"]) == ("cpu", "int8")
    assert second.kw["local_files_only"] is True and len(second.calls) == 2


def test_cuda_failure_while_running(fake_fw, tmp_path):
    eng = loaded(tmp_path)
    FakeModel.fail = RuntimeError("Library libcublas.so.12 is not found or cannot be loaded")
    with pytest.raises(EngineUnavailableError) as info:
        eng.run(req())
    assert "cpu" in str(info.value) and isinstance(info.value.__cause__, RuntimeError)


def test_other_runtime_errors_pass_through(fake_fw, tmp_path):
    eng = loaded(tmp_path)
    FakeModel.fail = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        eng.run(req())


def test_cuda_failure_while_loading(monkeypatch, tmp_path):
    class NoCuda:
        def __init__(self, path, **kw):
            raise ValueError("This CTranslate2 package was not compiled with CUDA support")
    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=NoCuda))
    with pytest.raises(EngineUnavailableError):
        loaded(tmp_path)


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
