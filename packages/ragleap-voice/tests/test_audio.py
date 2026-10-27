import array
import math
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from ragleap_voice import audio as a  # noqa: E402


def tone(n, rate, hz=440, amp=6000):
    return array.array("h", [int(amp * math.sin(2 * math.pi * hz * i / rate)) for i in range(n)])


def test_decode_table_properties():
    d = a.ulaw_decode(bytes(range(256)))
    assert len(d) == 256
    for u in range(256):
        assert d[u ^ 0x80] == -d[u]
    assert d[0xFF] == 0 and d[0x7F] == 0
    assert all(d[i] > d[i + 1] for i in range(0x80, 0xFF))   # positive half: value falls from the largest to 0 as the code rises
    assert all(d[i] < d[i + 1] for i in range(0x00, 0x7F))   # negative half: value rises from the most negative to 0


def test_encode_decode_roundtrip_of_codes():
    d = a.ulaw_decode(bytes(range(256)))
    back = a.ulaw_encode(d)
    assert [u for u in range(256) if back[u] != u] == [0x7F]  # the other zero code maps to 0xFF


def test_roundtrip_error_is_bounded():
    xs = array.array("h", range(-32768, 32768, 7))
    ys = a.ulaw_decode(a.ulaw_encode(xs))
    assert all(abs(x - y) <= abs(x) // 16 + 16 for x, y in zip(xs, ys))


def test_frames():
    s = array.array("h", range(10))
    assert [list(f) for f in a.frames(s, 4)] == [[0, 1, 2, 3], [4, 5, 6, 7]]
    assert [list(f) for f in a.frames(s, 4, "pad")][-1] == [8, 9, 0, 0]
    assert [list(f) for f in a.frames(s, 4, "keep")][-1] == [8, 9]
    assert list(a.frames(array.array("h"), 4, "keep")) == []
    for bad in (0, -1, True, 1.5):
        with pytest.raises(ValueError):
            list(a.frames(s, bad))
    with pytest.raises(ValueError):
        list(a.frames(s, 4, "x"))


def test_levels():
    assert a.rms(array.array("h")) == 0.0 and a.peak(array.array("h")) == 0
    assert a.rms_dbfs(array.array("h", [0, 0])) == -math.inf
    assert a.peak_dbfs(array.array("h", [-32768])) == pytest.approx(0.0)
    assert a.rms_dbfs(array.array("h", [150] * 160)) == pytest.approx(-46.79, abs=0.01)  # the old channel's threshold
    assert a.rms_dbfs(array.array("h", [16384] * 10)) == pytest.approx(-6.02, abs=0.01)


def test_gain_clips():
    assert list(a.gain(array.array("h", [1000, -1000, 30000, -30000]), 2.0)) == [2000, -2000, 32767, -32768]


def test_normalize_level():
    quiet = tone(1600, 8000, amp=100)
    out, db = a.normalize_level(quiet)
    assert a.rms_dbfs(out) == pytest.approx(-23.0, abs=0.3) and db > 0
    spiky = array.array("h", [0] * 99 + [10000])
    out, db = a.normalize_level(spiky)
    assert a.peak(out) <= 0.95 * 32768 + 1
    same, db0 = a.normalize_level(array.array("h", [0] * 8))
    assert list(same) == [0] * 8 and db0 == 0.0


def test_resampler_lengths_and_dc():
    dc = array.array("h", [1000] * 200)
    d = a.down2(dc)
    assert len(d) == 100 and all(abs(v - 1000) <= 2 for v in d[20:80])
    u = a.up2(array.array("h", [1000] * 100))
    assert len(u) == 200 and all(abs(v - 1000) <= 2 for v in u[40:160])
    assert len(a.down2(array.array("h", range(7)))) == 4 and len(a.down2(array.array("h"))) == 0


def test_tone_survives_resampling():
    t16 = tone(1600, 16000)
    t8 = a.down2(t16)
    assert abs(a.rms(t8[20:-20]) / a.rms(t16) - 1) < 0.05
    back = a.up2(t8)
    assert abs(a.rms(back[40:-40]) / a.rms(t16) - 1) < 0.08
