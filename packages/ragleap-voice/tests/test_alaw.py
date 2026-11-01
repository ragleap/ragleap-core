import array

import pytest

from ragleap_voice import audio as a

# Table 1a/1b, printed pages 3 and 4: (interval n, character signal after inversion of even bits is xor 0x55, quantized value)
PRINTED = ((0, 128, 1), (32, 160, 66), (48, 176, 132), (64, 192, 264), (80, 208, 528), (96, 224, 1056),
           (112, 240, 2112), (127, 255, 4032))
X = [0]
for w in [2] * 32 + [4] * 16 + [8] * 16 + [16] * 16 + [32] * 16 + [64] * 16 + [128] * 16:
    X.append(X[-1] + w)


def expected(s):
    n = max(i for i in range(129) if 8 * X[i] <= abs(s))
    n = min(n, 127)
    return (n if s < 0 else 128 + n) ^ 0x55


def test_decision_values_spot_checks():
    for n, v in ((32, 64), (33, 68), (48, 128), (49, 136), (64, 256), (65, 272), (81, 544), (97, 1088), (113, 2176),
                 (127, 3968), (128, 4096)):
        assert X[n] == v and a._ALAW_X8[n] == 8 * v


def test_printed_vectors():
    for n, ch, y in PRINTED:
        assert a.alaw_decode(bytes([ch ^ 0x55]))[0] == 8 * y
        assert a.alaw_decode(bytes([n ^ 0x55]))[0] == -8 * y
        assert a.alaw_encode([8 * y]) == bytes([ch ^ 0x55])
        assert a.alaw_encode([-8 * y]) == bytes([n ^ 0x55])


def test_known_codes():
    assert a.alaw_encode([0, -1, 16, -16]) == bytes([0xD5, 0x55, 0xD4, 0x54])
    assert a.alaw_decode(bytes([0xD5, 0x55])).tolist() == [8, -8]


def test_every_input_matches_independent_table_encoder():
    for s in range(-32768, 32768):
        assert a.alaw_encode([s]) == bytes([expected(s)]), s


def test_all_decision_values_both_signs_and_neighbours():
    for n in range(1, 128):
        b = 8 * X[n]
        for s in (b - 1, b, b + 1):
            for t in (s, -s):
                if -32768 <= t <= 32767:
                    assert a.alaw_encode([t]) == bytes([expected(t)])
        assert a.alaw_encode([b]) == bytes([(128 + n) ^ 0x55])
        assert a.alaw_encode([-b]) == bytes([n ^ 0x55])
        assert a.alaw_encode([b - 1]) == bytes([(128 + n - 1) ^ 0x55])


def test_extremes_and_symmetry():
    assert a.alaw_encode([32767]) == bytes([0xAA])
    assert a.alaw_encode([-32768]) == bytes([0x2A])
    for s in range(1, 32768):
        assert a.alaw_encode([s])[0] ^ a.alaw_encode([-s])[0] == 0x80


def test_decode_all_codes_and_round_trip():
    dec = a.alaw_decode(bytes(range(256)))
    assert len(dec) == 256 and max(dec) == 32256 and min(dec) == -32256 and len(set(dec)) == 256
    for c in range(256):
        assert a.alaw_encode([dec[c]]) == bytes([c])


def test_input_validation():
    for bad in (5, "ab", None):
        with pytest.raises(TypeError):
            a.alaw_decode(bad)
    for bad in (1.9, True, "1"):
        with pytest.raises(TypeError):
            a.alaw_encode([bad])
    for bad in (32768, -32769):
        with pytest.raises(ValueError):
            a.alaw_encode([bad])
    assert a.alaw_encode(array.array("h", [0, 1])) == a.alaw_encode([0, 1])
    assert a.alaw_decode(bytearray(b"\xd5")).tolist() == [8] and a.alaw_decode(memoryview(b"\xd5")).tolist() == [8]


def test_differs_from_audioop_only_at_negative_decision_values():
    audioop = pytest.importorskip("audioop")
    s = range(-32768, 32768)
    ref = audioop.lin2alaw(array.array("h", s).tobytes(), 2)
    ours = a.alaw_encode(s)
    diff = [v for v, x, y in zip(s, ours, ref) if x != y]
    assert sorted(diff) == sorted(-8 * X[n] for n in range(1, 128))
    dec = array.array("h")
    dec.frombytes(audioop.alaw2lin(bytes(range(256)), 2))
    assert a.alaw_decode(bytes(range(256))) == dec
