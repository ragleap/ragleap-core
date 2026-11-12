import asyncio
import os
import socket
import warnings

import pytest

from ragleap_phone import Capabilities, EndpointConfig, Frame, RtpMediaEndpoint, g711, rtp

SRC = ("10.0.0.5", 4000)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeTransport:
    def __init__(self):
        self.sent = []
        self.closed = False

    def sendto(self, data, addr):
        self.sent.append((data, addr))

    def close(self):
        self.closed = True

    def get_extra_info(self, name):
        return ("127.0.0.1", 40000) if name == "sockname" else None


def _cfg(**kw):
    base = dict(law="ulaw", remote_ip="10.0.0.5", remote_port=4000)
    base.update(kw)
    return EndpointConfig(**base)


def _pkt(seq, ssrc=7, payload=b"\xff" * 160):
    return rtp.build(0, seq, 160 * seq, ssrc, payload)


def _fake(holder):
    async def opener(loop, protocol_factory, host, port):
        holder["host"], holder["port"] = host, port
        holder["proto"], holder["t"] = protocol_factory(), FakeTransport()
        return holder["t"], holder["proto"]
    return opener


async def _started(ep, holder, on_call=None):
    got = []

    async def default(call):
        async for f in call.audio_in():
            got.append(f)
    task = asyncio.create_task(ep.run(on_call or default))
    for _ in range(100):
        if "t" in holder and ep.call is not None:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.02)
    return task, got


def test_the_interface_values():
    assert RtpMediaEndpoint.name == "rtp"
    caps = RtpMediaEndpoint.capabilities
    assert isinstance(caps, Capabilities) and caps.max_concurrent_calls == 1 and caps.sample_rates == (16000,)


def test_only_loopback_is_allowed_unless_the_operator_opts_in():
    RtpMediaEndpoint(_cfg())
    RtpMediaEndpoint(_cfg(), host="::1")
    for host in ("0.0.0.0", "192.168.1.5", "::"):
        with pytest.raises(ValueError):
            RtpMediaEndpoint(_cfg(), host=host)
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        RtpMediaEndpoint(_cfg(), host="192.168.1.5", allow_non_loopback=True)
    assert any(issubclass(x.category, RuntimeWarning) and "encrypted" in str(x.message) for x in w)
    for bad in (dict(host="not an ip"), dict(port=-1), dict(port=70000), dict(port=True)):
        with pytest.raises(ValueError):
            RtpMediaEndpoint(_cfg(), **bad)


def test_lifecycle_with_a_fake_transport():
    async def main():
        holder = {}
        ep = RtpMediaEndpoint(_cfg(), opener=_fake(holder), clock=Clock())
        task, got = await _started(ep, holder)
        assert holder["host"] == "127.0.0.1" and ep.local_address == ("127.0.0.1", 40000)
        holder["proto"].datagram_received(_pkt(1), SRC)
        holder["proto"].datagram_received(_pkt(2), ("10.0.0.9", 4000))
        await asyncio.sleep(0.05)
        await ep.close()
        await ep.close()
        await asyncio.wait_for(task, 2)
        assert holder["t"].closed and ep.call.ended
        assert sum(f.n_samples for f in got) == 320
        assert ep.call.counts["accepted"] == 1 and ep.call.counts["wrong_source"] == 1
        with pytest.raises(RuntimeError):
            await ep.run(lambda call: None)
    asyncio.run(main())


def test_one_packet_per_step_goes_to_the_configured_remote_and_a_partial_packet_is_flushed():
    async def main():
        holder = {}
        ep = RtpMediaEndpoint(_cfg(law="alaw"), opener=_fake(holder), clock=Clock(), step_seconds=1000)
        task, _ = await _started(ep, holder)
        for i in range(5):
            await ep.call.send_audio(Frame(bytes(640), 16000, i))
        t = holder["t"]
        for _ in range(4):
            ep.step()
        assert len(t.sent) == 4 and all(addr == ("10.0.0.5", 4000) for _, addr in t.sent)
        assert ep.call.has_partial_playback()
        for _ in range(5):
            ep.step()
        assert len(t.sent) == 4 and not ep.call.has_partial_playback()
        ep.step()
        assert len(t.sent) == 5
        last = rtp.parse(t.sent[-1][0], 8)
        assert len(last.payload) == 160 and last.payload[-1:] == g711.alaw_encode([0])
        seqs = [rtp.parse(d, 8).sequence for d, _ in t.sent]
        assert [(b - a) & 0xFFFF for a, b in zip(seqs, seqs[1:])] == [1, 1, 1, 1]
        await ep.close()
        await asyncio.wait_for(task, 2)
    asyncio.run(main())


def test_the_timeouts_are_checked_every_fiftieth_step():
    async def main():
        holder, clk = {}, Clock()
        ep = RtpMediaEndpoint(_cfg(idle_timeout_seconds=10), opener=_fake(holder), clock=clk, step_seconds=1000)
        task, _ = await _started(ep, holder)
        clk.t += 11
        for _ in range(60):
            ep.step()
        assert ep.call.ended
        await asyncio.wait_for(task, 2)
        assert holder["t"].closed
    asyncio.run(main())


def test_socket_errors_are_counted_without_their_text():
    async def main():
        holder = {}
        ep = RtpMediaEndpoint(_cfg(), opener=_fake(holder), clock=Clock(), step_seconds=1000)
        task, _ = await _started(ep, holder)
        holder["proto"].error_received(OSError("secret detail"))
        assert ep.socket_errors == 1
        await ep.close()
        await asyncio.wait_for(task, 2)
    asyncio.run(main())


def test_an_error_in_the_handler_surfaces():
    async def main():
        holder = {}
        ep = RtpMediaEndpoint(_cfg(), opener=_fake(holder), clock=Clock())

        async def bad(call):
            raise KeyError("boom")
        task = asyncio.create_task(ep.run(bad))
        with pytest.raises(KeyError):
            await asyncio.wait_for(task, 2)
        assert holder["t"].closed
    asyncio.run(main())


@pytest.mark.skipif(os.environ.get("RAGLEAP_PHONE_LOOPBACK") != "1", reason="opt-in: real UDP on 127.0.0.1 (RAGLEAP_PHONE_LOOPBACK=1)")
def test_real_udp_on_loopback():
    async def main():
        peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        peer.bind(("127.0.0.1", 0))
        peer.setblocking(False)
        ep = RtpMediaEndpoint(_cfg(remote_ip="127.0.0.1", remote_port=peer.getsockname()[1]))
        got = []

        async def on_call(call):
            async def reader():
                async for f in call.audio_in():
                    got.append(f)
            r = asyncio.create_task(reader())
            for i in range(5):
                await call.send_audio(Frame(bytes(640), 16000, i))
            await r

        task = asyncio.create_task(ep.run(on_call))
        for _ in range(100):
            if ep.local_address:
                break
            await asyncio.sleep(0.01)
        assert ep.local_address[0] == "127.0.0.1"
        loop = asyncio.get_running_loop()
        for s in (1, 2, 3):
            peer.sendto(_pkt(s), ep.local_address)
        data = await asyncio.wait_for(loop.sock_recv(peer, 2000), 2)
        assert rtp.parse(data, 0).payload_type == 0
        await asyncio.sleep(0.1)
        await ep.close()
        await asyncio.wait_for(task, 2)
        peer.close()
        assert sum(f.n_samples for f in got) == 2 * 160 * 3
    asyncio.run(main())
