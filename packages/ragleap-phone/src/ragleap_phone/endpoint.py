"""One phone call carried over RTP: the Call half of the media endpoint, with no socket (docs/design/phase0-rtp-endpoint.md,
sections 3 to 8). A transport layer calls on_datagram() for every datagram that arrives, tick() now and then, and pop_datagram()
every 20 ms to send; this class decides what to accept and keeps all the limits. It never logs and keeps only counters. Frames
given to send_audio() must be at 16 kHz. Not thread-safe: call everything from the event loop's thread. hangup() stops the media
and ends the Call object; it cannot end the phone call, which needs the signalling side.
"""

import array
import asyncio
import collections
import ipaddress
import secrets
import time
from dataclasses import dataclass

from . import g711, rtp
from .adapter import Call, Event
from .audiopath import InboundAudio, OutboundAudio

PACKET_BYTES = 160
PACKET_TICKS = 160
FILL_SLACK_SAMPLES = 16000  # 2 s: how far the audio handed on may run ahead of real time (a starting value)


def _pos_int(name, v, hi=None):
    if isinstance(v, bool) or not isinstance(v, int) or v < 1 or (hi is not None and v > hi):
        raise ValueError("%s must be an integer from 1%s" % (name, "" if hi is None else " to %d" % hi))


def _parse_remote(ip, port):
    try:
        addr = ipaddress.ip_address(ip)
    except (ValueError, TypeError):
        raise ValueError("remote_ip must be an IP address")
    _pos_int("remote_port", port, 65535)
    return addr, port


@dataclass(frozen=True)
class EndpointConfig:
    """Operator settings. The expected remote address and port come from the operator, never from packets: give them here, or leave
    both out and set them once with expect_remote() when they are only known later (Asterisk's externalMedia reports its media
    port after the channel exists). Until they are set every datagram is dropped."""

    law: str
    remote_ip: str = None
    remote_port: int = None
    max_packets_per_second: int = 100
    max_call_seconds: int = 3600
    idle_timeout_seconds: int = 10
    max_payload: int = rtp.MAX_PAYLOAD
    max_gap: int = rtp.MAX_GAP
    frame_queue_size: int = 500

    def __post_init__(self):
        if self.law not in rtp.PAYLOAD_TYPES:
            raise ValueError("law must be one of %s" % (sorted(rtp.PAYLOAD_TYPES),))
        if (self.remote_ip is None) != (self.remote_port is None):
            raise ValueError("remote_ip and remote_port must be given together, or both left out and set later with expect_remote()")
        if self.remote_ip is not None:
            ip, port = _parse_remote(self.remote_ip, self.remote_port)
            object.__setattr__(self, "remote_ip", str(ip))
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
        self._remote = None if config.remote_ip is None else _parse_remote(config.remote_ip, config.remote_port)
        self._rx = rtp.Receiver(rtp.PAYLOAD_TYPES[config.law], config.max_payload, config.max_gap)
        self._in = InboundAudio(config.law)
        self._out = OutboundAudio(config.law)
        self._own = {"wrong_source": 0, "remote_not_set": 0, "rate_limited": 0, "queue_full": 0, "after_end": 0, "fill_limited": 0}
        self._media_samples = 0
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

    @property
    def remote(self):
        """The expected remote (ip, port) as set by the operator, or None while it is not set."""
        return None if self._remote is None else (str(self._remote[0]), self._remote[1])

    def expect_remote(self, ip, port):
        """Set the expected remote address and port, once, when the configuration left them out. Nothing is ever learned from packets."""
        if self._ended:
            raise RuntimeError("the call has ended")
        if self._remote is not None:
            raise RuntimeError("the remote address is already set")
        self._remote = _parse_remote(ip, port)

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
            return ipaddress.ip_address(source[0]) == self._remote[0] and source[1] == self._remote[1]
        except (ValueError, TypeError, IndexError, KeyError):
            return False

    def on_datagram(self, datagram, source):
        """Handle one received datagram from `source`, a (ip, port) pair. Order: source, rate limit, then the packet rules."""
        if self._ended:
            self._own["after_end"] += 1
            return
        if self._remote is None:
            self._own["remote_not_set"] += 1
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
        packet, gap = got
        ts_ns = int(now * 1e9)
        if self._started_at is None:
            self._started_at = now
            self._emit("call.started")
        self._last_packet_at = now
        room = max(0, int((now - self._started_at) * rtp.CLOCK_RATE) + FILL_SLACK_SAMPLES - self._media_samples)
        if gap > room:
            gap = room
            self._own["fill_limited"] += 1
        self._media_samples += gap + len(packet.payload)
        if gap:
            self._put_frame(self._in.silence(gap, ts_ns))
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
        self._queue_packets()

    def _queue_packets(self):
        while len(self._pending) >= PACKET_BYTES:
            chunk = bytes(self._pending[:PACKET_BYTES])
            del self._pending[:PACKET_BYTES]
            self._send_q.append(rtp.build(self._pt, self._seq, self._ts, self._ssrc, chunk))
            self._seq = (self._seq + 1) & 0xFFFF
            self._ts = (self._ts + PACKET_TICKS) & 0xFFFFFFFF
            self._playing = True

    def has_partial_playback(self):
        """True while less than one whole packet of outbound audio is waiting."""
        return bool(self._pending)

    def flush_playback(self):
        """Pad the waiting partial packet with silence of the call's law and queue it. The transport calls this after a short quiet time."""
        if self._ended or not self._pending:
            return
        encode = g711.ulaw_encode if self.config.law == "ulaw" else g711.alaw_encode
        self._pending += encode(array.array("h", bytes(2 * (PACKET_BYTES - len(self._pending)))))
        self._queue_packets()

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
