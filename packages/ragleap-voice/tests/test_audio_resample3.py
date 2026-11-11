import array
import math
import random

import pytest

from ragleap_voice import audio

CASES = [(audio.Down3Stream, audio.down3), (audio.Up3Stream, audio.up3)]


def _noise(n):
    rnd = random.Random(1)
    return array.array("h", [rnd.randint(-20000, 20000) for _ in range(n)])


def _tone(f, fs, n, a=10000):
    return array.array("h", [int(a * math.sin(2 * math.pi * f * i / fs)) for i in range(n)])


def _rms(x):
    return math.sqrt(sum(v * v for v in x) / len(x))


@pytest.mark.parametrize("cls,fn", CASES)
@pytest.mark.parametrize("size", [1, 7, 100, 333, 1001])
def test_chunked_equals_one_shot(cls, fn, size):
    x = _noise(1001)
    s = cls()
    out = array.array("h")
    for i in range(0, len(x), size):
        out.extend(s.process(x[i:i + size]))
    out.extend(s.finish())
    assert out == fn(x)


@pytest.mark.parametrize("cls,fn", CASES)
def test_empty_chunks_and_finish_twice(cls, fn):
    s = cls()
    assert len(s.process([])) == 0
    s.process(_noise(50))
    s.finish()
    assert len(s.finish()) == 0
    with pytest.raises(ValueError):
        s.process([1])


def test_lengths():
    for n in (0, 1, 2, 3, 4, 100, 1001):
        x = _noise(n)
        assert len(audio.down3(x)) == -(-n // 3)
        assert len(audio.up3(x)) == 3 * n


def test_constant_level_is_kept():
    x = array.array("h", [1000] * 600)
    assert abs(audio.down3(x)[100] - 1000) <= 1
    assert abs(audio.up3(x)[600] - 1000) <= 1


def test_down3_passband_and_stopband():
    low = _tone(1000, 48000, 4800)
    high = _tone(12000, 48000, 4800)
    assert _rms(audio.down3(low)[40:-40]) / _rms(low[120:-120]) > 0.99
    assert _rms(audio.down3(high)[40:-40]) / _rms(high) < 0.01


def test_up3_passband():
    low = _tone(1000, 16000, 1600)
    assert _rms(audio.up3(low)[120:-120]) / _rms(low) > 0.99
