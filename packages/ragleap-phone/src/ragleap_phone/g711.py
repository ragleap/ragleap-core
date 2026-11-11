"""G.711 mu-law and A-law for the audio path of a phone adapter, standard library only (docs/design/phase0-adapter-mapping.md).

The code is copied from ragleap-voice's audio helpers, so that ragleap-phone does not depend on ragleap-voice. The same notes apply:
- Mu-law decode is table-driven. The mu-law encoder is NOT verified against the G.711 standard; it differs from Python's audioop in
  some negative inputs, always by one code. Do not rely on bit-exactness with any implementation.
- A-law follows ITU-T G.711 Tables 1a/1b, with the 16-bit scaling and the treatment of exact decision values chosen as in
  ragleap-voice (an exact tie goes to the upper-magnitude interval). It differs from audioop.lin2alaw on some negative values. The
  table values have not been checked against the ITU-T document by a person who has it. Not a conformance claim.
- Section 3.2 zero-code suppression is not implemented.
"""

import array
import bisect as _bisect

BIAS = 0x84


def _dec(u):
    u = ~u & 0xFF
    s = (((u & 0x0F) << 3) + BIAS) << ((u >> 4) & 7)
    s -= BIAS
    return -s if u & 0x80 else s


def _enc(s):
    sign = 0x80 if s < 0 else 0
    if s < 0:
        s = -s
    if s > 32635:
        s = 32635
    s += BIAS
    e = s.bit_length() - 8
    return ~(sign | (e << 4) | ((s >> (e + 3)) & 0x0F)) & 0xFF


_DEC = [_dec(u) for u in range(256)]
_ENC = bytes(_enc(i - 32768) for i in range(65536))


def ulaw_decode(codes):
    """G.711 mu-law bytes -> array('h') of 16-bit linear samples."""
    return array.array("h", [_DEC[b] for b in bytes(codes)])


def ulaw_encode(samples):
    """16-bit linear samples -> mu-law bytes (see the module note on exactness)."""
    return bytes([_ENC[s + 32768] for s in samples])


_ALAW_X = [0]
for _w in [2] * 32 + [4] * 16 + [8] * 16 + [16] * 16 + [32] * 16 + [64] * 16 + [128] * 16:
    _ALAW_X.append(_ALAW_X[-1] + _w)
_ALAW_X8 = tuple(8 * _x for _x in _ALAW_X)  # decision values on the 16-bit scale (Table 1a/1b, 13-bit x 8)
_ALAW_DEC = [0] * 256
for _n in range(128):
    _mid = 8 * ((_ALAW_X[_n] + _ALAW_X[_n + 1]) // 2)
    _ALAW_DEC[(128 + _n) ^ 0x55] = _mid
    _ALAW_DEC[_n ^ 0x55] = -_mid
_ALAW_DEC = tuple(_ALAW_DEC)


def alaw_encode(samples):
    """Encode signed 16-bit PCM ints to A-law bytes. Exact decision values go to the upper-magnitude interval."""
    out = bytearray()
    for s in samples:
        if isinstance(s, bool) or not isinstance(s, int):
            raise TypeError("samples must be integers")
        if not -32768 <= s <= 32767:
            raise ValueError("samples must be signed 16-bit integers")
        i = min(127, _bisect.bisect_right(_ALAW_X8, abs(s)) - 1)
        out.append((i if s < 0 else 128 + i) ^ 0x55)
    return bytes(out)


def alaw_decode(codes):
    """Decode A-law bytes (bytes, bytearray or memoryview) to array('h'). Each code decodes to its interval midpoint."""
    if not isinstance(codes, (bytes, bytearray, memoryview)):
        raise TypeError("codes must be bytes, bytearray or memoryview")
    return array.array("h", [_ALAW_DEC[c] for c in bytes(codes)])
