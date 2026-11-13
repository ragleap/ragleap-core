import array
import math
import random

import pytest

from ragleap_phone import Frame, Resampler, SUPPORTED_RATE_PAIRS

PAIRS = [(16000, 8000), (8000, 16000), (48000, 16000), (16000, 48000)]


def _noise(n, seed=1):
    rnd = random.Random(seed)
    return array.array("h", [rnd.randint(-20000, 20000) for _ in range(n)])


def _tone(f, fs, n, a=10000):
    return array.array("h", [int(a * math.sin(2 * math.pi * f * i / fs)) for i in range(n)])


def _rms(x):
    return math.sqrt(sum(v * v for v in x) / len(x))


def _frame(samples, rate, ts=0):
    return Frame(samples.tobytes(), rate, ts)


def _run(samples, a, b, size=None):
    """Convert in frames of `size` samples (or all at once) and return all output samples."""
    r = Resampler(a, b)
    out = array.array("h")
    step = size or max(len(samples), 1)
    for i in range(0, len(samples), step):
        out.extend(r.process(_frame(samples[i:i + step], a, i)).to_samples())
    out.extend(r.finish().to_samples())
    return out


def test_supported_pairs():
    assert set(SUPPORTED_RATE_PAIRS) == set(PAIRS)


@pytest.mark.parametrize("a,b", [(44100, 16000), (8000, 48000), (48000, 8000), (16000, 24000)])
def test_unsupported_conversions_are_refused(a, b):
    with pytest.raises(ValueError):
        Resampler(a, b)


@pytest.mark.parametrize("a,b", [(16000.0, 8000), (True, 16000), (16000, "8000")])
def test_rates_must_be_integers(a, b):
    with pytest.raises(ValueError):
        Resampler(a, b)


def test_same_rate_passes_frames_through():
    r = Resampler(16000, 16000)
    f = _frame(_noise(100), 16000, 7)
    assert r.process(f) is f
    assert r.finish().n_samples == 0


@pytest.mark.parametrize("a,b", PAIRS)
@pytest.mark.parametrize("size", [1, 7, 100, 333])
def test_chunked_equals_one_shot(a, b, size):
    x = _noise(1001)
    assert _run(x, a, b, size) == _run(x, a, b)


@pytest.mark.parametrize("a,b", PAIRS)
def test_output_length(a, b):
    for n in (1, 2, 3, 4, 100, 1001):
        x = _noise(n)
        want = -(-n // (a // b)) if a > b else n * (b // a)
        assert len(_run(x, a, b)) == want


@pytest.mark.parametrize("a,b", PAIRS)
def test_constant_level_is_kept_within_one_step(a, b):
    x = array.array("h", [1000] * 600)
    out = _run(x, a, b)
    assert abs(out[len(out) // 2] - 1000) <= 1


@pytest.mark.parametrize("a,b", PAIRS)
def test_a_one_kilohertz_tone_is_kept(a, b):
    x = _tone(1000, a, int(a * 0.3))
    out = _run(x, a, b)
    k = len(out) // 10
    j = int(k * a / b)
    assert _rms(out[k:-k]) / _rms(x[j:-j]) > 0.99


@pytest.mark.parametrize("a,b,f", [(16000, 8000, 6000), (48000, 16000, 12000)])
def test_a_tone_above_the_new_nyquist_is_removed(a, b, f):
    x = _tone(f, a, int(a * 0.3))
    out = _run(x, a, b)
    k = len(out) // 10
    assert _rms(out[k:-k]) / _rms(x) < 0.01


def test_wrong_input_rate_and_use_after_finish_are_refused():
    r = Resampler(16000, 8000)
    with pytest.raises(ValueError):
        r.process(_frame(_noise(10), 8000))
    r.process(_frame(_noise(10), 16000))
    r.finish()
    assert r.finish().n_samples == 0
    with pytest.raises(ValueError):
        r.process(_frame(_noise(10), 16000))


def test_output_frames_carry_rate_and_input_timestamp():
    r = Resampler(48000, 16000)
    out = r.process(_frame(_noise(960), 48000, 12345))
    assert out.sample_rate == 16000 and out.timestamp_ns == 12345
    assert r.finish().timestamp_ns == 12345


def test_matches_ragleap_voice_when_it_is_installed():
    audio = pytest.importorskip("ragleap_voice.audio")
    if not all(hasattr(audio, n) for n in ("down2", "up2", "down3", "up3")):
        pytest.skip("this ragleap_voice has no 3:1 helpers")
    x = _noise(1001)
    for (a, b), fn in zip(PAIRS, (audio.down2, audio.up2, audio.down3, audio.up3)):
        assert _run(x, a, b, 100) == fn(x), (a, b)


@pytest.mark.parametrize("a,b", PAIRS)
@pytest.mark.parametrize("n", [1, 30, 31, 32, 63, 64, 500])
def test_process_zeros_equals_filtering_zeros(a, b, n):
    history = _noise(777, seed=3)
    follow = _noise(400, seed=4)
    fast, slow = Resampler(a, b), Resampler(a, b)
    out_fast, out_slow = array.array("h"), array.array("h")
    out_fast.extend(fast.process(_frame(history, a)).to_samples())
    out_slow.extend(slow.process(_frame(history, a)).to_samples())
    out_fast.extend(fast.process_zeros(n, 9).to_samples())
    out_slow.extend(slow.process(_frame(array.array("h", [0] * n), a, 9)).to_samples())
    out_fast.extend(fast.process(_frame(follow, a)).to_samples())
    out_slow.extend(slow.process(_frame(follow, a)).to_samples())
    out_fast.extend(fast.finish().to_samples())
    out_slow.extend(slow.finish().to_samples())
    assert out_fast == out_slow
