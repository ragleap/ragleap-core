import array
import math
import random

import pytest

from ragleap_voice.errors import UnsupportedFeatureError
from ragleap_voice.gate import StreamGate

RATE = 8000
FRAME = 160  # 20 ms


def tone(frames, amp=8000):
    return array.array("h", [int(amp * math.sin(2 * math.pi * 1000 * n / RATE)) for n in range(frames * FRAME)])


def silence(frames):
    return array.array("h", [0] * (frames * FRAME))


def noise(frames, amp, seed=1):
    rng = random.Random(seed)
    return array.array("h", [rng.randint(-amp, amp) for _ in range(frames * FRAME)])


def run(signal, sizes=None, options=None):
    g = StreamGate(RATE, options)
    events, pos, k = [], 0, 0
    while pos < len(signal):
        size = (sizes[k % len(sizes)] if sizes else len(signal))
        events.extend(g.feed(signal[pos:pos + size]))
        pos, k = pos + size, k + 1
    events.extend(g.finish())
    return [(e.kind, e.frame, e.reason) for e in events]


def test_silence_only_is_no_speech():
    ev = run(silence(100))
    assert [e[0] for e in ev] == ["no_speech"] and ev[0][1] == 100


def test_quiet_noise_below_floor_is_no_speech():
    assert [e[0] for e in run(noise(100, 30))] == ["no_speech"]


def test_short_blip_is_not_an_utterance():
    assert [e[0] for e in run(tone(3) + silence(60))] == ["no_speech"]


def test_speech_then_silence():
    ev = run(tone(50) + silence(50))
    assert ev == [("speech_start", 0, "5 consecutive speech-like frames"), ("speech_end", 50, "silence")]


def test_speech_without_trailing_silence_ends_at_stream_end():
    ev = run(silence(10) + tone(30))
    assert [e[:2] for e in ev] == [("speech_start", 10), ("speech_end", 40)]
    assert ev[-1][2] == "stream_end"


def test_loud_stationary_noise_passes_known_limitation():
    ev = run(noise(60, 8000))
    assert ev[0][0] == "speech_start"


def test_two_utterances():
    ev = run(tone(20) + silence(40) + tone(20) + silence(40))
    assert [e[0] for e in ev] == ["speech_start", "speech_end", "speech_start", "speech_end"]


def test_quiet_speaker_after_loud_one_is_kept():
    ev = run(tone(20, 20000) + silence(40) + tone(20, 300) + silence(40))
    assert [e[0] for e in ev].count("speech_start") == 2


def test_hard_cap():
    ev = run(tone(100), options={"hard_cap_frames": 30})
    assert [(e[0], e[1]) for e in ev] == [
        ("speech_start", 0), ("speech_end", 30), ("speech_start", 30), ("speech_end", 60),
        ("speech_start", 60), ("speech_end", 90), ("speech_start", 90), ("speech_end", 100)]
    assert ev[1][2] == "hard_cap"


def test_chunking_does_not_change_events():
    sig = silence(7) + tone(40) + silence(45) + noise(30, 8000, 2) + silence(30)
    want = run(sig)
    for sizes in ([1], [FRAME], [320], [7, 0, 333, 1, 160], [FRAME - 1]):
        assert run(sig, sizes) == want


def test_finish_idempotent_and_feed_after_finish_raises():
    g = StreamGate(RATE)
    g.feed(silence(3))
    assert g.finish()
    assert g.finish() == []
    with pytest.raises(ValueError):
        g.feed(silence(1))


@pytest.mark.parametrize("rate,options", [(0, None), (True, None), (RATE, {"bogus": 1}),
                                           (RATE, {"hangover_frames": 0}), (RATE, {"hard_cap_frames": 5})])
def test_bad_arguments(rate, options):
    with pytest.raises(UnsupportedFeatureError):
        StreamGate(rate, options)
