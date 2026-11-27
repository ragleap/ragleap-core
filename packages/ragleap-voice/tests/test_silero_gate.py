import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from ragleap_voice import vad_silero as v  # noqa: E402
from ragleap_voice.errors import ModelNotFoundError, ModelNotVerifiedError, UnsupportedFeatureError  # noqa: E402
from test_gate import tone_wav  # noqa: E402
from test_transcribe import FakeStt, go, make_env  # noqa: E402

SILERO = {"model_path": "/models/silero.onnx"}


class Const:
    def __init__(self, p):
        self.p = p

    def prob(self, window):
        assert len(window) == v.WINDOW
        return self.p


@pytest.fixture
def backend(monkeypatch):
    holder = {"p": 0.9, "path": None}

    def fake_load(path):
        holder["path"] = path
        return Const(holder["p"])

    monkeypatch.setattr(v, "_load_backend", fake_load)
    return holder


def run(env, eng, **kw):
    kw.setdefault("audio", tone_wav())
    return go(env, eng, language="en", engine="fake", **kw)


def test_no_speech_returns_an_empty_transcript_without_running_the_engine(tmp_path, backend):
    backend["p"] = 0.0
    env, eng = make_env(tmp_path), FakeStt()
    t = run(env, eng, gate="silero", gate_options=SILERO)
    assert t.text == "" and t.segments == () and t.model_status == "verified" and t.engine == "fake"
    assert eng.loaded is None and eng.request is None
    assert any("silero gate: no speech detected" in n and "engine was not run" in n for n in t.notes)
    assert backend["path"] == SILERO["model_path"]


def test_speech_passes_and_the_engine_runs(tmp_path, backend):
    backend["p"] = 0.9
    env, eng = make_env(tmp_path), FakeStt()
    t = run(env, eng, gate="silero", gate_options=SILERO)
    assert t.text == "hello" and eng.request is not None
    assert any("silero gate: passed" in n for n in t.notes)


def test_detector_options_reach_the_detector(tmp_path, backend):
    backend["p"] = 0.9
    env = make_env(tmp_path)
    assert run(env, FakeStt(), gate="silero", gate_options={**SILERO, "threshold": 0.95}).text == ""
    assert run(env, FakeStt(), gate="silero", gate_options={**SILERO, "threshold": 0.5}).text == "hello"


def test_option_errors_come_before_any_model_work(tmp_path, backend):
    env, eng = make_env(tmp_path), FakeStt()
    for opts in (None, {}, {"model_path": ""}, {"model_path": 3}, {"threshold": 0.5}, {**SILERO, "nope": 1}):
        with pytest.raises(UnsupportedFeatureError):
            run(env, eng, gate="silero", gate_options=opts)
    assert eng.loaded is None and eng.request is None


def test_wrong_audio_format_is_refused_before_the_engine(tmp_path, backend):
    env, eng = make_env(tmp_path), FakeStt()
    for wav in (tone_wav(channels=2), tone_wav(rate=8000)):
        with pytest.raises(UnsupportedFeatureError):
            run(env, eng, audio=wav, gate="silero", gate_options=SILERO)
    assert eng.request is None


def test_a_missing_or_wrong_model_file_is_refused(tmp_path):
    env = make_env(tmp_path)
    with pytest.raises(ModelNotFoundError):
        run(env, FakeStt(), gate="silero", gate_options={"model_path": str(tmp_path / "absent.onnx")})
    bad = tmp_path / "bad.onnx"
    bad.write_bytes(b"not the model")
    with pytest.raises(ModelNotVerifiedError):
        run(env, FakeStt(), gate="silero", gate_options={"model_path": str(bad)})


def test_off_is_unchanged_and_an_unknown_mode_still_fails(tmp_path):
    env, eng = make_env(tmp_path), FakeStt()
    t = run(env, eng, gate="off")
    assert t.text == "hello" and not any("gate" in n for n in t.notes)
    with pytest.raises(ValueError):
        run(env, FakeStt(), gate="maybe")
