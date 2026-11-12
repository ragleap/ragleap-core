"""Audio path of a phone adapter: G.711 payload bytes <-> Frame objects at the rate ragleap-voice expects.

InboundAudio turns what a caller sends (8 kHz G.711 bytes) into Frames at 16 kHz (or 8 kHz). OutboundAudio turns Frames at 16 kHz
or 8 kHz into the 8 kHz G.711 bytes to send back. Both are stateful, so the filter history carries over between packets.
Packet size is not enforced (a 20 ms packet is 160 bytes at 8 kHz). Nothing here touches a network.
"""

from . import g711
from .frame import Frame
from .resample import Resampler, _to_pcm

LAWS = ("ulaw", "alaw")
_DECODE = {"ulaw": g711.ulaw_decode, "alaw": g711.alaw_decode}
_ENCODE = {"ulaw": g711.ulaw_encode, "alaw": g711.alaw_encode}
PHONE_RATE = 8000


def _check_law(law):
    if law not in LAWS:
        raise ValueError("law must be one of %s" % (LAWS,))


class InboundAudio:
    """G.711 bytes from the caller -> Frame at out_rate (16000 or 8000). Not thread-safe."""

    def __init__(self, law, out_rate=16000):
        _check_law(law)
        self._decode = _DECODE[law]
        self._rs = Resampler(PHONE_RATE, out_rate)

    def process(self, payload, timestamp_ns):
        """Decode one packet; the Frame holds the samples that are complete now and carries timestamp_ns."""
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise TypeError("payload must be bytes, bytearray or memoryview")
        decoded = self._decode(bytes(payload))
        return self._rs.process(Frame(_to_pcm(decoded), PHONE_RATE, timestamp_ns))

    def silence(self, n_samples, timestamp_ns):
        """Feed n_samples of digital silence at 8 kHz, for packets that were lost. Independent of the law."""
        return self._rs.process(Frame(bytes(2 * n_samples), PHONE_RATE, timestamp_ns))

    def finish(self):
        """Flush the filter tail as a last Frame (possibly empty)."""
        return self._rs.finish()


class OutboundAudio:
    """Frames at in_rate (16000 or 8000) -> 8 kHz G.711 bytes for the caller. Not thread-safe."""

    def __init__(self, law, in_rate=16000):
        _check_law(law)
        self._encode = _ENCODE[law]
        self._rs = Resampler(in_rate, PHONE_RATE)

    def process(self, frame):
        """Encode one Frame; returns the bytes that are complete now."""
        return self._encode(self._rs.process(frame).to_samples())

    def finish(self):
        """Flush the filter tail as the last bytes (possibly empty)."""
        return self._encode(self._rs.finish().to_samples())
