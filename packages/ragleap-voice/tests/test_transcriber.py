import sys
import threading
import time

import pytest

import ragleap_voice as rv
from ragleap_voice import EngineError, RagleapVoiceError, Transcriber, UnsupportedFeatureError
from ragleap_voice.results import Transcript
from ragleap_voice.whisper_engine import MODEL
from test_transcribe import make_env, wav_bytes  # noqa: F401
from test_whisper_engine import FakeModel, fake_fw  # noqa: F401


@pytest.fixture
def tr(fake_fw, tmp_path):
    env = make_env(tmp_path, status="unverified", name=MODEL)
    t = Transcriber(manifest=env[0])
    yield t, env[1]
    t.close()


def call(t, cache, **kw):
    return t.transcribe(wav_bytes(), language="en", allow_unverified=True, cache_dir=cache, **kw)


def test_model_opened_once_across_calls(tr):
    t, cache = tr
    assert call(t, cache).text == "hello world"
    call(t, cache)
    call(t, cache)
    assert len(FakeModel.instances) == 1 and len(FakeModel.instances[0].calls) == 3


def test_closed_transcriber_rejects_calls(tr):
    t, cache = tr
    call(t, cache)
    t.close()
    t.close()
    with pytest.raises(RagleapVoiceError):
        call(t, cache)


def test_context_manager_closes(fake_fw, tmp_path):
    env = make_env(tmp_path, status="unverified", name=MODEL)
    with Transcriber(manifest=env[0]) as t:
        call(t, env[1])
    with pytest.raises(RagleapVoiceError):
        call(t, env[1])


def test_input_errors_keep_the_model(tr):
    t, cache = tr
    call(t, cache)
    with pytest.raises(UnsupportedFeatureError):
        call(t, cache, options={"pitch": 1})
    call(t, cache)
    assert len(FakeModel.instances) == 1


def test_runtime_failure_drops_the_model(tr):
    t, cache = tr
    call(t, cache)
    FakeModel.fail = RuntimeError("boom")
    with pytest.raises(EngineError):
        call(t, cache)
    FakeModel.fail = None
    call(t, cache)
    assert len(FakeModel.instances) == 2


def test_calls_are_serialised(tr, monkeypatch):
    t, cache = tr
    state = {"active": 0, "max": 0}
    real = FakeModel.transcribe

    def slow(self, audio, **kw):
        state["active"] += 1
        state["max"] = max(state["max"], state["active"])
        time.sleep(0.05)
        try:
            return real(self, audio, **kw)
        finally:
            state["active"] -= 1
    monkeypatch.setattr(FakeModel, "transcribe", slow)
    threads = [threading.Thread(target=lambda: call(t, cache)) for _ in range(4)]
    [x.start() for x in threads]
    [x.join() for x in threads]
    assert state["max"] == 1 and len(FakeModel.instances) == 1


def test_close_waits_for_a_call_in_progress(tr, monkeypatch):
    t, cache = tr
    started, order = threading.Event(), []
    real = FakeModel.transcribe

    def slow(self, audio, **kw):
        started.set()
        time.sleep(0.2)
        order.append("call done")
        return real(self, audio, **kw)
    monkeypatch.setattr(FakeModel, "transcribe", slow)
    th = threading.Thread(target=lambda: call(t, cache))
    th.start()
    started.wait(2)
    t.close()
    order.append("closed")
    th.join()
    assert order == ["call done", "closed"]


def test_registry_engine_is_not_shared(tr):
    t, cache = tr
    assert t._engine is not rv.engines  # sanity: private engine object
    from ragleap_voice.registry import _DEFAULT
    assert t._engine is not _DEFAULT.get("whisper")


def test_transcribe_stream_uses_the_transcriber():
    seen = []

    class Fake:
        def transcribe(self, wav, **kw):
            seen.append(kw)
            return Transcript("hi", "en", "whisper", "m", "unverified", (), ())
    ev = list(rv.transcribe_stream(iter([b"\x01\x00" * 800]), gate="off", transcriber=Fake(), language="en"))
    assert [e.type for e in ev] == ["stream.started", "transcript.final"] and seen[0]["timestamps"] is True


def test_other_input_errors_keep_the_model(tr):
    t, cache = tr
    call(t, cache)
    with pytest.raises(ValueError):
        t.transcribe(wav_bytes(), language="", allow_unverified=True, cache_dir=cache)
    with pytest.raises(rv.UnsupportedLanguageError):
        t.transcribe(wav_bytes(), language="zz", allow_unverified=True, cache_dir=cache)
    call(t, cache)
    assert len(FakeModel.instances) == 1


def test_closed_transcriber_cannot_be_entered_again(tr):
    t, cache = tr
    t.close()
    with pytest.raises(RagleapVoiceError):
        with t:
            pass
