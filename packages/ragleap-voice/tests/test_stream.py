import array
import io
import math
import wave

import pytest

from ragleap_voice.errors import EngineError, RagleapVoiceError
from ragleap_voice.results import Segment, Transcript
from ragleap_voice.stream import TranscriptStream

RATE = 16000
FRAME = 320


def tone(frames, amp=8000):
    return array.array("h", [int(amp * math.sin(2 * math.pi * 1000 * n / RATE)) for n in range(frames * FRAME)]).tobytes()


def silence(frames):
    return bytes(frames * FRAME * 2)


class Runner:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def __call__(self, wav):
        self.calls.append(wav)
        if self.fail:
            raise EngineError("boom", hint="check the log")
        return Transcript("hello", "en", "fake", "m", "unverified", (Segment(0.0, 1.0, "hello"),), ("n",))


def stream(runner=None, **kw):
    return TranscriptStream(RATE, runner=runner or Runner(), **kw)


def types(events):
    return [e.type for e in events]


def check_envelope(events):
    assert [e.sequence for e in events] == list(range(len(events)))
    assert len({e.stream_id for e in events}) == 1
    terminal = [e for e in events if e.type in ("transcript.final", "stream.no_speech", "stream.cancelled", "stream.error")]
    assert len(terminal) == 1 and events[-1] is terminal[0]


def test_speech_gives_started_segment_final():
    r = Runner()
    s = stream(r)
    s.feed(tone(50) + silence(40))
    s.finish()
    ev = list(s)
    assert types(ev) == ["stream.started", "transcript.segment", "transcript.final"]
    assert ev[-1].data["text"] == "hello" and ev[-1].data["model_status"] == "unverified"
    check_envelope(ev)
    with wave.open(io.BytesIO(r.calls[0])) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == (1, 2, RATE, 90 * FRAME)


def test_silence_is_no_speech_and_engine_not_run():
    r = Runner()
    s = stream(r)
    s.feed(silence(100))
    s.finish()
    ev = list(s)
    assert types(ev) == ["stream.started", "stream.no_speech"] and r.calls == []
    check_envelope(ev)


def test_gate_off_runs_engine_on_silence():
    r = Runner()
    s = stream(r, gate="off")
    s.feed(silence(10))
    s.finish()
    assert types(list(s))[-1] == "transcript.final" and len(r.calls) == 1


def test_no_audio_is_no_speech():
    s = stream(gate="off")
    s.finish()
    assert types(list(s)) == ["stream.started", "stream.no_speech"]


def test_engine_error_becomes_one_error_event():
    s = stream(Runner(fail=True))
    s.feed(tone(50))
    s.finish()
    ev = list(s)
    assert types(ev) == ["stream.started", "stream.error"]
    assert ev[-1].data["error_type"] == "EngineError" and ev[-1].data["hint"] == "check the log"
    check_envelope(ev)


def test_cancel_before_iteration():
    r = Runner()
    s = stream(r)
    s.feed(tone(50))
    s.cancel()
    s.cancel()
    ev = list(s)
    assert types(ev) == ["stream.started", "stream.cancelled"] and r.calls == []
    check_envelope(ev)
    with pytest.raises(RagleapVoiceError):
        s.feed(tone(1))


def test_cancel_after_finish_discards_result():
    r = Runner()
    s = stream(r)
    s.feed(tone(50))
    s.finish()
    s.cancel()
    assert types(list(s)) == ["stream.started", "stream.cancelled"]


def test_feed_after_finish_rejected_and_finish_idempotent():
    s = stream()
    s.feed(tone(50))
    s.finish()
    s.finish()
    with pytest.raises(RagleapVoiceError):
        s.feed(tone(1))
    assert types(list(s)).count("transcript.final") == 1


def test_iterating_before_finish_is_misuse():
    s = stream()
    s.feed(tone(5))
    with pytest.raises(RagleapVoiceError):
        iter(s)


def test_odd_byte_chunks_are_reassembled():
    r = Runner()
    s = stream(r)
    data = tone(50)
    for i in range(0, len(data), 7):
        s.feed(data[i:i + 7])
    s.finish()
    list(s)
    with wave.open(io.BytesIO(r.calls[0])) as w:
        assert w.readframes(w.getnframes()) == data


def test_half_sample_at_end_is_decode_error():
    s = stream()
    s.feed(tone(50) + b"\x01")
    s.finish()
    ev = list(s)
    assert ev[-1].type == "stream.error" and ev[-1].data["error_type"] == "AudioDecodeError"


def test_input_limit_rejects_block_and_keeps_stream_usable():
    s = stream(max_bytes=1000)
    s.feed(bytes(600))
    with pytest.raises(RagleapVoiceError):
        s.feed(bytes(600))
    s.feed(bytes(400))


def test_chunks_iterable_mode():
    r = Runner()
    s = TranscriptStream(RATE, chunks=iter([tone(25), tone(25), silence(40)]), runner=r)
    ev = list(s)
    assert types(ev) == ["stream.started", "transcript.segment", "transcript.final"]
    check_envelope(ev)


def test_bad_arguments():
    for kw in ({"gate": "x"}, {"max_bytes": 0}):
        with pytest.raises(ValueError):
            stream(**kw)
    with pytest.raises(ValueError):
        TranscriptStream(0, runner=Runner())
