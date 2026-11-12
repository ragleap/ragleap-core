"""UDP transport for the RTP media endpoint (docs/design/phase0-rtp-endpoint.md, sections 4 and 7). Standard library only.

RtpMediaEndpoint carries ONE call: it listens on a UDP port, hands every datagram to an RtpCall (which checks the source, the rate and
the packet rules), sends one outbound packet every 20 ms to the configured remote and checks the timeouts once a second. It listens
on 127.0.0.1 unless the operator opts in to another address, and then it warns that the media is neither encrypted nor
cryptographically authenticated. It never logs; errors are only counted. Not tested against real PBX traffic. The unit tests use a
fake transport; one loopback test with a real socket is opt-in (RAGLEAP_PHONE_LOOPBACK=1).
"""

import asyncio
import ipaddress
import time
import warnings

from .adapter import Adapter, Capabilities
from .endpoint import EndpointConfig, RtpCall  # noqa: F401

STEP_SECONDS = 0.02
TICK_EVERY = 50
FLUSH_AFTER_IDLE_STEPS = 5


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, endpoint):
        self._endpoint = endpoint

    def datagram_received(self, data, addr):
        call = self._endpoint.call
        if call is not None:
            call.on_datagram(data, tuple(addr[:2]))

    def error_received(self, exc):
        self._endpoint.socket_errors += 1


async def _default_open(loop, protocol_factory, host, port):
    return await loop.create_datagram_endpoint(protocol_factory, local_addr=(host, port))


class RtpMediaEndpoint(Adapter):
    """A media endpoint for one call. `opener` is for tests: an async function (loop, protocol_factory, host, port) -> (transport, protocol)."""

    name = "rtp"
    capabilities = Capabilities("rtp-udp", ("ulaw", "alaw"), (16000,), False, True, 1)

    def __init__(self, config, host="127.0.0.1", port=0, allow_non_loopback=False, clock=time.monotonic,
                 opener=None, step_seconds=STEP_SECONDS):
        try:
            ip = ipaddress.ip_address(host)
        except (ValueError, TypeError):
            raise ValueError("host must be an IP address")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("port must be an integer from 0 to 65535")
        if not ip.is_loopback:
            if not allow_non_loopback:
                raise ValueError("refusing to listen on a non-loopback address without allow_non_loopback=True")
            warnings.warn("RTP media on a non-loopback address is neither encrypted nor cryptographically authenticated; "
                          "protect the network path", RuntimeWarning, stacklevel=2)
        self.config = config
        self._host, self._port = str(ip), port
        self._clock = clock
        self._opener = opener or _default_open
        self._step_seconds = step_seconds
        self.call = None
        self.local_address = None
        self.socket_errors = 0
        self._transport = None
        self._closing = False
        self._wake = None
        self._steps = 0
        self._idle = 0

    def step(self):
        """One 20 ms turn: send at most one packet, flush a partial packet after a quiet time, check the timeouts every 50th turn."""
        call = self.call
        if call is None or call.ended or self._transport is None:
            return
        self._steps += 1
        datagram = call.pop_datagram()
        if datagram is not None:
            self._idle = 0
            self._transport.sendto(datagram, (call.config.remote_ip, call.config.remote_port))
        elif call.has_partial_playback():
            self._idle += 1
            if self._idle >= FLUSH_AFTER_IDLE_STEPS:
                self._idle = 0
                call.flush_playback()
        else:
            self._idle = 0
        if self._steps % TICK_EVERY == 0:
            call.tick()
        if call.ended and self._wake is not None:
            self._wake.set()

    async def run(self, on_call):
        """Open the socket, hand the one RtpCall to on_call, and run until the call ends or close() is called."""
        if self.call is not None:
            raise RuntimeError("run() was already called")
        loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self.call = RtpCall(self.config, self._clock)
        self._transport, _ = await self._opener(loop, lambda: _Protocol(self), self._host, self._port)
        name = self._transport.get_extra_info("sockname")
        self.local_address = tuple(name[:2]) if name else None
        handler = loop.create_task(on_call(self.call))
        try:
            next_t = loop.time()
            while not self.call.ended and not self._closing and not handler.done():
                self.step()
                next_t += self._step_seconds
                try:
                    await asyncio.wait_for(self._wake.wait(), max(0.0, next_t - loop.time()))
                except asyncio.TimeoutError:
                    pass
        finally:
            await self.call.hangup()
            self._transport.close()
        await handler

    async def close(self):
        """Stop the media: end the call and close the socket. Safe to call more than once."""
        self._closing = True
        if self._wake is not None:
            self._wake.set()
        if self.call is not None:
            await self.call.hangup()
        if self._transport is not None:
            self._transport.close()
