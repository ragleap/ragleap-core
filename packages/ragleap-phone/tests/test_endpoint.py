import asyncio

import pytest

from ragleap_phone import Frame, rtp
from ragleap_phone.audiopath import InboundAudio
from ragleap_phone.endpoint import EndpointConfig, RtpCall

SRC = ("10.0.0.5", 4000)
PAYLOAD = b"\xff" * 160


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _cfg(**kw):
    base = dict(law="ulaw", remote_ip="10.0.0.5", remote_port=4000)
    base.update(kw)
    return EndpointConfig(**base)


def _pkt(seq, ssrc=7, pt=0, payload=PAYLOAD):
    return rtp.build(pt, seq, 160 * seq, ssrc, payload)


def _run(coro):
    return asyncio.run(coro)


async def _drain(call):
    frames = [f async for f in call.audio_in()]
    events = [e async for e in call.events]
    return frames, events


def test_config_is_checked():
    assert _cfg(remote_ip="::1").remote_ip == "::1"
    for bad in (dict(law="mulaw"), dict(remote_ip="not an ip"), dict(remote_ip=None), dict(remote_port=0), dict(remote_port=70000),
                dict(remote_port=True), dict(max_packets_per_second=0), dict(idle_timeout_seconds=-1), dict(max_payload=481),
                dict(max_call_seconds=1.5), dict(frame_queue_size=0)):
        with pytest.raises(ValueError):
            _cfg(**bad)


def test_a_wrong_source_is_dropped_before_parsing():
    async def main():
        c = RtpCall(_cfg(), Clock())
        c.on_datagram(b"garbage", ("10.0.0.6", 4000))
        c.on_datagram(_pkt(1), ("10.0.0.5", 4001))
        c.on_datagram(_pkt(1), None)
        c.on_datagram(_pkt(1), ("nonsense", 4000))
        assert c.counts["wrong_source"] == 4 and c.counts["too_short"] == 0 and c.counts["accepted"] == 0
        c.on_datagram(_pkt(1), SRC)
        assert c.counts["accepted"] == 1
    _run(main())


def test_audio_and_events_of_a_short_call():
    async def main():
        clk = Clock()
        c = RtpCall(_cfg(), clk)
        for s in range(1, 11):
            c.on_datagram(_pkt(s), SRC)
            clk.t += 0.02
        await c.hangup()
        frames, events = await _drain(c)
        assert sum(f.n_samples for f in frames) == 2 * 160 * 10
        assert all(f.sample_rate == 16000 for f in frames)
        assert [e.kind for e in events] == ["call.started", "call.ended"] and events[1].data == "hangup"
        assert c.counts["accepted"] == 10
    _run(main())


def test_a_gap_is_filled_with_silence():
    async def main():
        c = RtpCall(_cfg(), Clock())
        c.on_datagram(_pkt(1), SRC)
        c.on_datagram(_pkt(4), SRC)
        await c.hangup()
        frames, _ = await _drain(c)
        assert sum(f.n_samples for f in frames) == 2 * 160 * (2 + 2)
    _run(main())


def test_the_rate_limit_holds_within_a_second_and_resets_after():
    async def main():
        clk = Clock()
        c = RtpCall(_cfg(max_packets_per_second=5), clk)
        for s in range(1, 9):
            c.on_datagram(_pkt(s), SRC)
        assert c.counts["accepted"] == 5 and c.counts["rate_limited"] == 3
        clk.t += 1.0
        c.on_datagram(_pkt(9), SRC)
        assert c.counts["accepted"] == 6
    _run(main())


def test_idle_and_maximum_length_timeouts():
    async def main():
        clk = Clock()
        c = RtpCall(_cfg(idle_timeout_seconds=10, max_call_seconds=100), clk)
        c.on_datagram(_pkt(1), SRC)
        clk.t += 9.9
        assert c.tick() is False
        clk.t += 0.2
        assert c.tick() is True and c.ended and c.tick() is False
        _, events = await _drain(c)
        assert events[-1].kind == "call.ended" and events[-1].data == "idle_timeout"

        clk2 = Clock()
        c2 = RtpCall(_cfg(idle_timeout_seconds=10, max_call_seconds=100), clk2)
        for s in range(1, 13):
            c2.on_datagram(_pkt(s), SRC)
            clk2.t += 9.0
        assert c2.tick() is True
        _, ev2 = await _drain(c2)
        assert ev2[-1].data == "max_length"

        clk3 = Clock()
        c3 = RtpCall(_cfg(idle_timeout_seconds=10), clk3)
        clk3.t += 11
        assert c3.tick() is True
    _run(main())


def test_after_the_end_everything_is_refused_or_counted():
    async def main():
        c = RtpCall(_cfg(), Clock())
        await c.hangup()
        await c.hangup()
        c.on_datagram(_pkt(1), SRC)
        assert c.counts["after_end"] == 1 and c.counts["accepted"] == 0
        with pytest.raises(RuntimeError):
            await c.send_audio(Frame(bytes(640), 16000, 0))
        _, events = await _drain(c)
        assert [e.kind for e in events] == ["call.ended"]
    _run(main())


def test_a_full_frame_queue_drops_and_counts():
    async def main():
        c = RtpCall(_cfg(frame_queue_size=3), Clock())
        for s in range(1, 20):
            c.on_datagram(_pkt(s), SRC)
        assert c.counts["queue_full"] > 0 and c.counts["accepted"] == 19
        await c.hangup()
        frames, _ = await _drain(c)
        assert len(frames) <= 3
    _run(main())


def test_outbound_packets_have_a_regular_header():
    async def main():
        c = RtpCall(_cfg(law="alaw"), Clock())
        for i in range(5):
            await c.send_audio(Frame(bytes(640), 16000, i))
        got = []
        while True:
            d = c.pop_datagram()
            if d is None:
                break
            got.append(rtp.parse(d, 8))
        assert len(got) == 4
        assert all(len(p.payload) == 160 and p.payload_type == 8 for p in got)
        assert [(b.sequence - a.sequence) & 0xFFFF for a, b in zip(got, got[1:])] == [1, 1, 1]
        assert [(b.timestamp - a.timestamp) & 0xFFFFFFFF for a, b in zip(got, got[1:])] == [160, 160, 160]
        assert len({p.ssrc for p in got}) == 1
        await c.hangup()
        _, events = await _drain(c)
        assert [e.kind for e in events] == ["playback.finished", "call.ended"]
    _run(main())


def test_clear_playback_drops_queued_packets_without_a_finished_event():
    async def main():
        c = RtpCall(_cfg(), Clock())
        for i in range(5):
            await c.send_audio(Frame(bytes(640), 16000, i))
        await c.clear_playback()
        assert c.pop_datagram() is None
        await c.hangup()
        _, events = await _drain(c)
        assert [e.kind for e in events] == ["call.ended"]
    _run(main())


def test_calls_have_distinct_ids_and_counters_hold_only_numbers():
    async def main():
        a, b = RtpCall(_cfg(), Clock()), RtpCall(_cfg(), Clock())
        assert a.id != b.id and len(a.id) == 16 and a.direction == "inbound"
        a.on_datagram(_pkt(1, payload=b"secret-audio" * 10), SRC)
        assert all(isinstance(v, int) for v in a.counts.values())
    _run(main())


def test_inbound_silence_is_law_independent_zeros():
    for law in ("ulaw", "alaw"):
        ia = InboundAudio(law)
        out = ia.silence(160, 5).to_samples()
        out.extend(ia.finish().to_samples())
        assert len(out) == 320 and not any(out)


def test_flush_playback_pads_the_partial_packet_with_silence_of_the_law():
    from ragleap_phone import g711
    for law, enc in (("ulaw", g711.ulaw_encode), ("alaw", g711.alaw_encode)):
        async def main():
            c = RtpCall(_cfg(law=law), Clock())
            for i in range(5):
                await c.send_audio(Frame(bytes(640), 16000, i))
            assert c.has_partial_playback()
            n = 0
            while c.pop_datagram() is not None:
                n += 1
            assert n == 4
            c.flush_playback()
            d = rtp.parse(c.pop_datagram(), rtp.PAYLOAD_TYPES[law])
            assert len(d.payload) == 160 and d.payload[-1:] == enc([0]) and not c.has_partial_playback()
            c.flush_playback()
            assert c.pop_datagram() is None
        _run(main())


def test_silence_suppression_is_filled_from_the_timestamps_up_to_the_cap():
    async def main():
        c = RtpCall(_cfg(), Clock())
        pk = lambda seq, ts: rtp.build(0, seq, ts, 7, PAYLOAD)
        c.on_datagram(pk(1, 160), SRC)
        c.on_datagram(pk(2, 320), SRC)
        c.on_datagram(pk(3, 320 + 16000), SRC)
        await c.hangup()
        frames, _ = await _drain(c)
        assert sum(f.n_samples for f in frames) == 2 * (3 * 160 + 8000)
        assert c.counts["long_gaps"] == 1 and c.counts["accepted"] == 3
    _run(main())


def test_one_sender_cannot_make_the_gap_fill_expensive():
    async def main():
        c = RtpCall(_cfg(), Clock())
        seq = 1
        for _ in range(100):
            c.on_datagram(_pkt(seq), SRC)
            seq += 51
        await c.hangup()
        return c.counts
    counts = _run(main())
    assert counts["accepted"] == 100 and counts["queue_full"] == 0 and counts["long_gaps"] == 0


def test_the_remote_can_be_left_out_and_set_once_later():
    async def main():
        c = RtpCall(EndpointConfig(law="ulaw"), Clock())
        assert c.remote is None
        c.on_datagram(b"garbage", SRC)
        c.on_datagram(_pkt(1), SRC)
        assert c.counts["remote_not_set"] == 2 and c.counts["too_short"] == 0 and c.counts["accepted"] == 0
        c.expect_remote("10.0.0.5", 4000)
        assert c.remote == ("10.0.0.5", 4000)
        c.on_datagram(_pkt(1), SRC)
        c.on_datagram(_pkt(2), ("10.0.0.5", 4001))
        assert c.counts["accepted"] == 1 and c.counts["wrong_source"] == 1
        with pytest.raises(RuntimeError):
            c.expect_remote("10.0.0.6", 5000)
        assert c.remote == ("10.0.0.5", 4000)
    _run(main())


def test_expect_remote_is_checked_and_refused_after_the_end():
    async def main():
        for ip, port in (("not an ip", 4000), (None, 4000), ("10.0.0.5", 0), ("10.0.0.5", 70000), ("10.0.0.5", True)):
            c = RtpCall(EndpointConfig(law="ulaw"), Clock())
            with pytest.raises(ValueError):
                c.expect_remote(ip, port)
            assert c.remote is None
        c = RtpCall(EndpointConfig(law="ulaw"), Clock())
        await c.hangup()
        with pytest.raises(RuntimeError):
            c.expect_remote("10.0.0.5", 4000)
    _run(main())


def test_the_config_takes_both_remote_values_or_none():
    assert EndpointConfig(law="alaw").remote_ip is None
    with pytest.raises(ValueError):
        EndpointConfig(law="ulaw", remote_ip="10.0.0.5")
    with pytest.raises(ValueError):
        EndpointConfig(law="ulaw", remote_port=4000)
    assert EndpointConfig("ulaw", "10.0.0.5", 4000).remote_port == 4000


def test_the_audio_handed_on_cannot_run_ahead_of_real_time():
    async def main():
        clk = Clock()
        c = RtpCall(_cfg(), clk)
        ts = 160
        for s in range(1, 101):
            c.on_datagram(rtp.build(0, s, ts, 7, PAYLOAD), SRC)
            ts += 160 + 60000          # every packet claims a huge silence before it
        await c.hangup()
        frames, _ = await _drain(c)
        total_8k = sum(f.n_samples for f in frames) // 2
        assert total_8k <= 100 * 160 + 16000 and c.counts["fill_limited"] > 0
    _run(main())


def test_a_legitimate_silence_after_real_time_has_passed_is_still_filled():
    async def main():
        clk = Clock()
        c = RtpCall(_cfg(), clk)
        c.on_datagram(rtp.build(0, 1, 160, 7, PAYLOAD), SRC)
        clk.t += 6.0                                   # six seconds really pass
        c.on_datagram(rtp.build(0, 2, 160 + 160 + 8000, 7, PAYLOAD), SRC)   # one second of silence was not sent
        await c.hangup()
        frames, _ = await _drain(c)
        assert sum(f.n_samples for f in frames) == 2 * (160 + 8000 + 160) and c.counts["fill_limited"] == 0
    _run(main())
