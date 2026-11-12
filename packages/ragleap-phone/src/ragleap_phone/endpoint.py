"""One phone call carried over RTP: the Call half of the media endpoint, with no socket (docs/design/phase0-rtp-endpoint.md,
sections 3 to 8). A transport layer calls on_datagram() for every datagram that arrives, tick() now and then, and pop_datagram()
every 20 ms to send; this class decides what to accept and keeps all the limits. It never logs and keeps only counters. Frames
given to send_audio() must be at 16 kHz. Not thread-safe: call everything from the event loop's thread. hangup() stops the media
and ends the Call object; it cannot end the phone call, which needs the signalling side.
"""

import asyncio
import collections
import ipaddress
import secrets
import time
from dataclasses import dataclass

from . import rtp
from .adapter import Call, Event
from .audiopath import InboundAudio, OutboundAudio

PACKET_BYTES = 160
PACKET_TICKS = 160


def _pos_int(name, v, hi=None):
    if isinstance(v, bool) or not isinstance(v, int) or v < 1 or (hi is not None and v > hi):
        raise ValueError("%s must be an integer from 1%s" % (name, "" if hi is None else " to %d" % hi))


@dataclass(frozen=True)
class EndpointConfig:
    """Operator settings. The expected remote address and port are required: nothing is learned from packets."""

    law: str
    remote_ip: str
    remote_port: int
    max_packets_per_second: int = 100
    max_call_seconds: int = 3600
    idle_timeout_seconds: int = 10
    max_payload: int = rtp.MAX_PAYLOAD
    max_gap: int = rtp.MAX_GAP
    frame_queue_size: int = 500

    def __post_init__(self):
        if self.law not in rtp.PAYLOAD_TYPES:
            raise ValueError("law must be one of %s" % (sorted(rtp.PAYLOAD_TYPES),))
        try:
            ip = ipaddress.ip_address(self.remote_ip)
        except (ValueError, TypeError):
            raise ValueError("remote_ip must be an IP address")
        object.__setattr__(self, "remote_ip", str(ip))
        _pos_int("remote_port", self.remote_port, 65535)
        _pos_int("max_packets_per_second", self.max_packets_per_second)
        _pos_int("max_call_seconds", self.max_call_seconds)
        _pos_int("idle_timeout_seconds", self.idle_timeout_seconds)
        _pos_int("max_payload", self.max_payload, rtp.MAX_PAYLOAD)
        _pos_int("max_gap", self.max_gap)
        _pos_int("frame_queue_size", self.frame_queue_size)


class RtpCall(Call):
    """The Call for one RTP stream. Use one object per call and read audio_in and events from one task each."""

    def __init__(self, config, clock=time.monotonic, direction="inbound"):
        super().__init__(direction)
        self.config = config
        self._clock = clock
        self._ip = ipaddress.ip_address(config.remote_ip)
        self._rx = rtp.Receiver(rtp.PAYLOAD_TYPES[config.law], config.max_payload, config.max_gap)
        self._in = InboundAudio(config.law)
        self._out = OutboundAudio(config.law)
        self._own = {"wrong_source": 0, "rate_limited": 0, "queue_full": 0, "after_end": 0}
        now = clock()
        self._win_start, self._win_n = now, 0
        self._started_at = None
        self._last_packet_at = now
        self._ended = False
        self._frames = asyncio.Queue(maxsize=config.frame_queue_size)
        self._events = asyncio.Queue()
        self._send_q = collections.deque()
        self._pending = bytearray()
        self._playing = False
        self._pt = rtp.PAYLOAD_TYPES[config.law]
        self._seq = secrets.randbits(16)
        self._ts = secrets.randbits(32)
        self._ssrc = secrets.randbits(32)

    @property
    def counts(self):
        """Integer counters only: accepted packets and drops by reason. Never any packet content."""
        return {**self._rx.counts, **self._own}

    @property
    def ended(self):
        return self._ended

    def _ns(self):
        return int(self._clock() * 1e9)

    def _emit(self, kind, data=None):
        self._events.put_nowait(Event(kind, self._ns(), data))

    def _put_frame(self, frame):
        if frame.n_samples == 0:
            return
        try:
            self._frames.put_nowait(frame)
        except asyncio.QueueFull:
            self._own["queue_full"] += 1

    @staticmethod
    def _close(queue):
        while True:
            try:
                queue.put_nowait(None)
                return
            except asyncio.QueueFull:
                queue.get_nowait()

    def _end(self, reason):
        if self._ended:
            return
        self._ended = True
        self._put_frame(self._in.finish())
        self._emit("call.ended", reason)
        self._close(self._frames)
        self._close(self._events)

    def _source_ok(self, source):
        try:
            return ipaddress.ip_address(source[0]) == self._ip and source[1] == self.config.remote_port
        except (ValueError, TypeError, IndexError, KeyError):
            return False

    def on_datagram(self, datagram, source):
        """Handle one received datagram from `source`, a (ip, port) pair. Order: source, rate limit, then the packet rules."""
        if self._ended:
            self._own["after_end"] += 1
            return
        if not self._source_ok(source):
            self._own["wrong_source"] += 1
            return
        now = self._clock()
        if now - self._win_start >= 1.0:
            self._win_start, self._win_n = now, 0
        self._win_n += 1
        if self._win_n > self.config.max_packets_per_second:
            self._own["rate_limited"] += 1
            return
        got = self._rx.handle(datagram)
        if got is None:
            return
        packet, missing = got
        ts_ns = int(now * 1e9)
        if self._started_at is None:
            self._started_at = now
            self._emit("call.started")
        self._last_packet_at = now
        if missing:
            self._put_frame(self._in.silence(missing * PACKET_BYTES, ts_ns))
        self._put_frame(self._in.process(packet.payload, ts_ns))

    def tick(self):
        """Check the timeouts. Returns True when this call ended the call. The transport calls it about once a second."""
        if self._ended:
            return False
        now = self._clock()
        if self._started_at is not None and now - self._started_at >= self.config.max_call_seconds:
            self._end("max_length")
            return True
        if now - self._last_packet_at >= self.config.idle_timeout_seconds:
            self._end("idle_timeout")
            return True
        return False

    def pop_datagram(self):
        """The next datagram to send to the configured remote, or None. The transport calls it every 20 ms."""
        if not self._send_q:
            return None
        d = self._send_q.popleft()
        if not self._send_q and self._playing:
            self._playing = False
            self._emit("playback.finished")
        return d

    async def audio_in(self):
        while True:
            frame = await self._frames.get()
            if frame is None:
                return
            yield frame

    async def send_audio(self, frame):
        if self._ended:
            raise RuntimeError("the call has ended")
        self._pending += self._out.process(frame)
        while len(self._pending) >= PACKET_BYTES:
            chunk = bytes(self._pending[:PACKET_BYTES])
            del self._pending[:PACKET_BYTES]
            self._send_q.append(rtp.build(self._pt, self._seq, self._ts, self._ssrc, chunk))
            self._seq = (self._seq + 1) & 0xFFFF
            self._ts = (self._ts + PACKET_TICKS) & 0xFFFFFFFF
            self._playing = True

    async def clear_playback(self):
        self._send_q.clear()
        self._pending.clear()
        self._out = OutboundAudio(self.config.law)
        self._playing = False

    async def hangup(self):
        self._end("hangup")

    @property
    def events(self):
        async def gen():
            while True:
                event = await self._events.get()
                if event is None:
                    return
                yield event
        return gen()
