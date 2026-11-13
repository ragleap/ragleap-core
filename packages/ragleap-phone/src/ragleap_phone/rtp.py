"""RTP packet parsing and building for G.711 audio (docs/design/phase0-rtp-endpoint.md, sections 2, 3 and 6). Standard library only.

Pure functions and one small state machine: no sockets, no clock, no logging. Layout of the fixed header: RFC 3550 section 5.1,
static payload types: RFC 3551 (read in secondary sources, the RFCs themselves not read). Tested with hand-built byte strings, not
with real captures, so this shows self-consistency and not interoperability. Packets with a header extension are refused.
"""

import struct
from dataclasses import dataclass

PAYLOAD_TYPES = {"ulaw": 0, "alaw": 8}
CLOCK_RATE = 8000
MAX_DATAGRAM = 1500
MAX_PAYLOAD = 480
MAX_GAP = 50
GAP_UNIT = 160
MAX_DROPOUT = 3000
MAX_MISORDER = 100

PARSE_REASONS = ("too_short", "too_long", "bad_version", "bad_header_length", "extension", "bad_padding",
                 "wrong_payload_type", "payload_size")
STATE_REASONS = ("ssrc_changed", "duplicate_or_late", "jump_dropped")


class RtpError(ValueError):
    """A datagram that cannot be accepted. `reason` is one of PARSE_REASONS and holds no packet content."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class RtpPacket:
    payload_type: int
    marker: bool
    sequence: int
    timestamp: int
    ssrc: int
    payload: bytes


def parse(datagram, payload_type, max_payload=MAX_PAYLOAD):
    """Parse one datagram or raise RtpError. The payload type must equal the one the operator configured."""
    if not isinstance(datagram, (bytes, bytearray, memoryview)):
        raise TypeError("datagram must be bytes, bytearray or memoryview")
    d = bytes(datagram)
    if len(d) < 12:
        raise RtpError("too_short")
    if len(d) > MAX_DATAGRAM:
        raise RtpError("too_long")
    b0, b1 = d[0], d[1]
    if b0 >> 6 != 2:
        raise RtpError("bad_version")
    padding, extension, cc = (b0 >> 5) & 1, (b0 >> 4) & 1, b0 & 0x0F
    header_len = 12 + 4 * cc
    if header_len > len(d):
        raise RtpError("bad_header_length")
    if extension:
        raise RtpError("extension")
    end = len(d)
    if padding:
        n = d[-1]
        if n == 0 or header_len + n > len(d):
            raise RtpError("bad_padding")
        end -= n
    if b1 & 0x7F != payload_type:
        raise RtpError("wrong_payload_type")
    payload = d[header_len:end]
    if not 1 <= len(payload) <= max_payload:
        raise RtpError("payload_size")
    sequence, timestamp, ssrc = struct.unpack("!HII", d[2:12])
    return RtpPacket(b1 & 0x7F, bool(b1 >> 7), sequence, timestamp, ssrc, payload)


def build(payload_type, sequence, timestamp, ssrc, payload, marker=False):
    """Build one datagram: version 2, no padding, no extension, no CSRC."""
    for name, v, hi in (("payload_type", payload_type, 127), ("sequence", sequence, 0xFFFF),
                        ("timestamp", timestamp, 0xFFFFFFFF), ("ssrc", ssrc, 0xFFFFFFFF)):
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= hi:
            raise ValueError("%s must be an integer from 0 to %d" % (name, hi))
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise TypeError("payload must be bytes, bytearray or memoryview")
    payload = bytes(payload)
    if not 1 <= len(payload) <= MAX_PAYLOAD:
        raise ValueError("payload must hold 1 to %d octets" % MAX_PAYLOAD)
    return struct.pack("!BBHII", 0x80, (0x80 if marker else 0) | payload_type, sequence, timestamp, ssrc) + payload


def seq_delta(new, last):
    """Signed distance from `last` to `new` modulo 65536, in -32768..32767. Positive means newer."""
    d = (new - last) & 0xFFFF
    return d - 0x10000 if d >= 0x8000 else d


class Receiver:
    """The per-packet rules of the design, sections 3 and 6. handle() returns (packet, gap) for an accepted datagram and None for a
    dropped one; every outcome is counted in `counts`. `gap` is the number of 8 kHz samples that were not received just before this
    packet, worked out from the timestamps (so it covers lost packets and also silence suppression), capped at max_gap packets of
    160 samples (a longer gap is also counted as long_gaps). The first valid datagram fixes the SSRC of the call. A sequence number
    up to MAX_DROPOUT ahead is accepted; one up to MAX_MISORDER behind is dropped as late (no reorder buffer); a larger jump in
    either direction is dropped until a second packet follows it consecutively, and then the call is resynchronised (counted as
    resyncs). The two limits are starting values. Not thread-safe."""

    def __init__(self, payload_type, max_payload=MAX_PAYLOAD, max_gap=MAX_GAP):
        if payload_type not in PAYLOAD_TYPES.values():
            raise ValueError("payload_type must be one of %s" % (sorted(PAYLOAD_TYPES.values()),))
        self.payload_type = payload_type
        self.max_payload = max_payload
        self.max_gap = max_gap
        self.counts = {r: 0 for r in PARSE_REASONS + STATE_REASONS}
        self.counts.update(accepted=0, long_gaps=0, resyncs=0)
        self._ssrc = None
        self._last_seq = None
        self._last_ts = 0
        self._last_len = 0
        self._probation = None

    def _accept(self, p, gap):
        self._last_seq, self._last_ts, self._last_len = p.sequence, p.timestamp, len(p.payload)
        self.counts["accepted"] += 1
        return p, gap

    def handle(self, datagram):
        try:
            p = parse(datagram, self.payload_type, self.max_payload)
        except RtpError as e:
            self.counts[e.reason] += 1
            return None
        if self._ssrc is None:
            self._ssrc = p.ssrc
            return self._accept(p, 0)
        if p.ssrc != self._ssrc:
            self.counts["ssrc_changed"] += 1
            return None
        delta = seq_delta(p.sequence, self._last_seq)
        if 0 < delta <= MAX_DROPOUT:
            self._probation = None
            ts_diff = (p.timestamp - self._last_ts) & 0xFFFFFFFF
            if ts_diff >= 0x80000000:
                ts_diff -= 0x100000000
            gap = max(0, ts_diff - self._last_len)
            cap = self.max_gap * GAP_UNIT
            if gap > cap:
                self.counts["long_gaps"] += 1
                gap = cap
            return self._accept(p, gap)
        if -MAX_MISORDER <= delta <= 0:
            self.counts["duplicate_or_late"] += 1
            return None
        if self._probation == p.sequence:
            self._probation = None
            self.counts["resyncs"] += 1
            return self._accept(p, 0)
        self._probation = (p.sequence + 1) & 0xFFFF
        self.counts["jump_dropped"] += 1
        return None
