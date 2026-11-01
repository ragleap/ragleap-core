import io
import wave

import pytest

from ragleap_voice.errors import EngineError
from ragleap_voice.results import Speech
from ragleap_voice.synth_stream import SpeechStream

RATE = 16000


def make_speech(frames=1000, rate=RATE, width=2, channels=1):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(bytes((i * 7) % 256 for i in range(frames * channels * width)))
    return Speech(buf.getvalue(), rate, frames / rate, "fake", "m", "unverified", notes=("n",))


def pcm_of(speech):
    with wave.open(io.BytesIO(speech.wav)) as w:
        return w.readframes(w.getnframes())


def check(ev):
    assert [e.sequence for e in ev] == list(range(len(ev)))
    assert len({e.stream_id for e in ev}) == 1
    assert sum(e.type in ("audio.completed", "stream.cancelled", "stream.error") for e in ev) == 1
    assert ev[-1].type in ("audio.completed", "stream.cancelled", "stream.error")


def test_chunks_reassemble_exactly():
    sp = make_speech(1000)
    ev = list(SpeechStream(lambda: sp))
    check(ev)
    chunks = [e for e in ev if e.type == "audio.chunk"]
    assert [c.data["index"] for c in chunks] == list(range(len(chunks))) and len(chunks) == 4
    assert b"".join(c.data["audio"] for c in chunks) == pcm_of(sp)
    assert all(c.data["sample_rate"] == RATE and c.data["channels"] == 1 and c.data["sample_format"] == "s16le" for c in chunks)
    assert ev[-1].type == "audio.completed" and ev[-1].data["chunks"] == 4 and ev[-1].data["synthetic"] is True


def test_stereo_chunks_stay_frame_aligned():
    sp = make_speech(1000, channels=2)
    ev = list(SpeechStream(lambda: sp, chunk_ms=10))
    chunks = [e.data["audio"] for e in ev if e.type == "audio.chunk"]
    assert all(len(c) % 4 == 0 for c in chunks) and b"".join(chunks) == pcm_of(sp)


def test_empty_waveform_completes_with_zero_chunks():
    ev = list(SpeechStream(lambda: make_speech(0)))
    assert [e.type for e in ev] == ["stream.started", "audio.completed"] and ev[-1].data["chunks"] == 0


def test_cancel_before_start_does_not_run_engine():
    calls = []
    s = SpeechStream(lambda: calls.append(1) or make_speech())
    s.cancel()
    ev = list(s)
    assert [e.type for e in ev] == ["stream.started", "stream.cancelled"] and calls == []
    check(ev)


def test_cancel_midway_stops_chunks():
    s = SpeechStream(lambda: make_speech(2000))
    seen = []
    for e in s:
        seen.append(e)
        if e.type == "audio.chunk" and e.data["index"] == 1:
            s.cancel()
    check(seen)
    assert seen[-1].type == "stream.cancelled" and sum(e.type == "audio.chunk" for e in seen) == 2


def test_engine_error_is_one_error_event():
    def boom():
        raise EngineError("boom", hint="log")
    ev = list(SpeechStream(boom))
    assert [e.type for e in ev] == ["stream.started", "stream.error"]
    assert ev[-1].data["error_type"] == "EngineError" and ev[-1].data["hint"] == "log"


def test_oversize_waveform_is_error():
    ev = list(SpeechStream(lambda: make_speech(1000), max_bytes=1000))
    assert ev[-1].type == "stream.error" and not any(e.type == "audio.chunk" for e in ev)


def test_non_16bit_is_unsupported():
    ev = list(SpeechStream(lambda: make_speech(100, width=1)))
    assert ev[-1].type == "stream.error" and ev[-1].data["error_type"] == "UnsupportedFeatureError"


def test_bad_arguments():
    for kw in ({"chunk_ms": 0}, {"max_bytes": 0}, {"chunk_ms": True}):
        with pytest.raises(ValueError):
            SpeechStream(lambda: None, **kw)
