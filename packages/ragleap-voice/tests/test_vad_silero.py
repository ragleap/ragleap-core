import array
import hashlib
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from ragleap_voice import vad_silero as v  # noqa: E402
from ragleap_voice.errors import (EngineNotInstalledError, ModelNotFoundError, ModelNotVerifiedError,  # noqa: E402
                                  UnsupportedFeatureError)


class Fake:
    def __init__(self, probs):
        self.probs = list(probs)
        self.seen = 0

    def prob(self, window):
        assert len(window) == v.WINDOW
        p = self.probs[self.seen] if self.seen < len(self.probs) else 0.0
        self.seen += 1
        return p


def pcm(windows):
    return array.array("h", [0]) * (v.WINDOW * windows)


def gate(probs, options=None):
    return v.SileroGate(16000, None, options, _backend=Fake(probs))


def kinds(events):
    return [(e.kind, e.frame) for e in events]


def test_speech_starts_after_three_windows_and_reports_the_first_one():
    g = gate([0.1, 0.9, 0.9, 0.9, 0.9])
    ev = g.feed(pcm(5))
    assert kinds(ev) == [("speech_start", 1)]
    assert ev[0].time_s == pytest.approx(0.032)


def test_two_windows_are_not_enough():
    g = gate([0.9, 0.9, 0.1, 0.9, 0.9, 0.1])
    assert g.feed(pcm(6)) == []
    assert kinds(g.finish()) == [("no_speech", 6)]


def test_speech_ends_after_the_hangover():
    g = gate([0.9] * 3 + [0.0] * 12)
    ev = g.feed(pcm(15))
    assert kinds(ev) == [("speech_start", 0), ("speech_end", 3)]
    assert ev[1].reason == "silence"


def test_hard_cap_ends_a_long_utterance():
    g = gate([0.9] * 8, {"min_speech_windows": 2, "hard_cap_windows": 5, "hangover_windows": 50})
    ev = g.feed(pcm(8))
    assert kinds(ev)[:2] == [("speech_start", 0), ("speech_end", 5)]
    assert ev[1].reason == "hard_cap"


def test_chunk_size_does_not_change_the_events():
    probs = [0.1, 0.9, 0.9, 0.9, 0.9, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.9]
    one = gate(probs)
    whole = one.feed(pcm(18)) + one.finish()
    two = gate(probs)
    data = pcm(18)
    parts = []
    for i in range(0, len(data), 300):
        parts += two.feed(data[i:i + 300])
    parts += two.finish()
    assert kinds(parts) == kinds(whole)


def test_open_utterance_is_closed_at_the_end_of_the_stream():
    g = gate([0.9] * 4)
    assert kinds(g.feed(pcm(4))) == [("speech_start", 0)]
    ev = g.finish()
    assert kinds(ev) == [("speech_end", 4)] and ev[0].reason == "stream_end"
    assert g.finish() == []


def test_feed_after_finish_is_refused():
    g = gate([])
    g.finish()
    with pytest.raises(ValueError):
        g.feed(pcm(1))


@pytest.mark.parametrize("rate", [8000, 48000, True, 16000.0])
def test_only_16000_hz_is_accepted(rate):
    with pytest.raises(UnsupportedFeatureError):
        v.SileroGate(rate, None, _backend=Fake([]))


@pytest.mark.parametrize("opts", [{"threshold": 1.5}, {"threshold": 0}, {"min_speech_windows": 0}, {"hangover_windows": True},
                                  {"min_speech_windows": 5, "hard_cap_windows": 5}, {"nonsense": 1}])
def test_bad_options_are_refused(opts):
    with pytest.raises(UnsupportedFeatureError):
        gate([], opts)


def test_missing_model_file(tmp_path):
    with pytest.raises(ModelNotFoundError):
        v.SileroGate(16000, tmp_path / "nope.onnx")


def test_wrong_file_is_refused_before_anything_is_loaded(tmp_path, monkeypatch):
    f = tmp_path / "other.onnx"
    f.write_bytes(b"not the pinned model")
    monkeypatch.setattr(v, "_build_backend", lambda raw: pytest.fail("a refused file must not be loaded"))
    with pytest.raises(ModelNotVerifiedError):
        v.SileroGate(16000, f)


def test_pinned_file_is_loaded(tmp_path, monkeypatch):
    f = tmp_path / "model.onnx"
    f.write_bytes(b"stand-in model bytes")
    monkeypatch.setattr(v, "MODEL_SHA256", hashlib.sha256(b"stand-in model bytes").hexdigest())
    seen = {}
    monkeypatch.setattr(v, "_build_backend", lambda raw: seen.setdefault("backend", Fake([0.9] * 3)))
    g = v.SileroGate(16000, f)
    assert kinds(g.feed(pcm(3))) == [("speech_start", 0)] and seen["backend"].seen == 3


def test_missing_extra_gives_a_clear_error(tmp_path, monkeypatch):
    f = tmp_path / "model.onnx"
    f.write_bytes(b"stand-in model bytes")
    monkeypatch.setattr(v, "MODEL_SHA256", hashlib.sha256(b"stand-in model bytes").hexdigest())

    def no_module(name):
        raise ImportError(name)
    monkeypatch.setattr(v, "_import", no_module)
    with pytest.raises(EngineNotInstalledError) as exc:
        v.SileroGate(16000, f)
    assert "ragleap-voice[vad]" in str(exc.value) or "ragleap-voice[vad]" in str(getattr(exc.value, "hint", ""))


def test_the_pinned_hash_is_the_benchmark_one():
    assert v.MODEL_SHA256 == "7ed98ddbad84ccac4cd0aeb3099049280713df825c610a8ed34543318f1b2c49"
