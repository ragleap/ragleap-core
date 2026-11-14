import random

import pytest

from ragleap_phone import rtp
from ragleap_phone.rtp import Receiver, RtpError, build, parse, seq_delta

PCMU, PCMA = 0, 8
HDR = bytes.fromhex("80000001000000a011223344")  # v2, PT 0, seq 1, ts 160, ssrc 0x11223344, written by hand


def _reason(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except RtpError as e:
        return e.reason
    return None


def _pkt(seq, ssrc=0x11223344, pt=PCMU, payload=b"\xff" * 160):
    return build(pt, seq, 160 * seq, ssrc, payload)


def test_build_matches_a_hand_written_header():
    assert build(PCMU, 1, 160, 0x11223344, b"\xff" * 160) == HDR + b"\xff" * 160
    assert build(PCMA, 1, 160, 0x11223344, b"\x55", marker=True)[:2] == bytes([0x80, 0x88])


def test_parse_reads_a_hand_written_packet():
    p = parse(HDR + b"abc", PCMU)
    assert (p.payload_type, p.marker, p.sequence, p.timestamp, p.ssrc, p.payload) == (0, False, 1, 160, 0x11223344, b"abc")
    assert parse(bytes([0x80, 0x88]) + HDR[2:] + b"x", PCMA).marker is True


def test_round_trip_at_the_edges():
    for seq, ts, ssrc in ((0, 0, 0), (65535, 0xFFFFFFFF, 0xFFFFFFFF)):
        p = parse(build(PCMA, seq, ts, ssrc, b"z" * 480), PCMA)
        assert (p.sequence, p.timestamp, p.ssrc, len(p.payload)) == (seq, ts, ssrc, 480)


def test_csrc_list_and_padding_are_skipped():
    csrc = bytes(8)
    d = bytes([0x82]) + HDR[1:] + csrc + b"abcd"
    assert parse(d, PCMU).payload == b"abcd"
    d = bytes([0xA0]) + HDR[1:] + b"abcd" + bytes([0, 0, 0, 4])
    assert parse(d, PCMU).payload == b"abcd"


@pytest.mark.parametrize("datagram,reason", [
    (HDR[:11], "too_short"),
    (HDR + b"a" * 1489, "too_long"),
    (bytes([0x40]) + HDR[1:] + b"a", "bad_version"),
    (bytes([0x00]) + HDR[1:] + b"a", "bad_version"),
    (bytes([0x81]) + HDR[1:], "bad_header_length"),
    (bytes([0x90]) + HDR[1:] + b"abcdefgh", "extension"),
    (bytes([0xA0]) + HDR[1:] + b"abc" + bytes([0]), "bad_padding"),
    (bytes([0xA0]) + HDR[1:] + b"abc" + bytes([9]), "bad_padding"),
    (HDR, "payload_size"),
    (HDR + b"a" * 481, "payload_size"),
    (bytes([0xA0]) + HDR[1:] + bytes([1]), "payload_size"),
    (bytes([0x80, 0x08]) + HDR[2:] + b"a", "wrong_payload_type"),
    (bytes([0x80, 0x65]) + HDR[2:] + b"a", "wrong_payload_type"),
])
def test_each_refusal_has_its_reason(datagram, reason):
    assert _reason(parse, datagram, PCMU) == reason


def test_wrong_input_types_and_build_limits():
    with pytest.raises(TypeError):
        parse("text", PCMU)
    with pytest.raises(TypeError):
        build(PCMU, 1, 1, 1, "text")
    for bad in (dict(sequence=65536), dict(sequence=-1), dict(timestamp=2 ** 32), dict(ssrc=True), dict(payload_type=128)):
        kw = dict(payload_type=PCMU, sequence=1, timestamp=1, ssrc=1, payload=b"a")
        kw.update(bad)
        with pytest.raises(ValueError):
            build(**kw)
    with pytest.raises(ValueError):
        build(PCMU, 1, 1, 1, b"")
    with pytest.raises(ValueError):
        build(PCMU, 1, 1, 1, b"a" * 481)


def test_random_datagrams_never_raise_anything_but_rtp_error():
    rnd = random.Random(7)
    for _ in range(3000):
        n = rnd.choice([0, 1, 11, 12, 13, 20, 40, 160, 500, 1500, 1501, 2000])
        d = bytes(rnd.randrange(256) for _ in range(n))
        if n >= 2 and rnd.random() < 0.7:
            d = bytes([0x80 | (rnd.randrange(4) if rnd.random() < 0.3 else 0) | (0x20 if rnd.random() < 0.3 else 0), rnd.choice([0, 8, 9])]) + d[2:]
        try:
            p = parse(d, PCMU)
        except RtpError as e:
            assert e.reason in rtp.PARSE_REASONS
        else:
            assert 1 <= len(p.payload) <= rtp.MAX_PAYLOAD


@pytest.mark.parametrize("new,last,delta", [(5, 4, 1), (4, 5, -1), (0, 65535, 1), (65535, 0, -1), (5, 5, 0), (32768, 0, -32768), (32767, 0, 32767)])
def test_seq_delta_wraps(new, last, delta):
    assert seq_delta(new, last) == delta


def test_receiver_accepts_in_order_across_the_wrap():
    r = Receiver(PCMU)
    got = [r.handle(_pkt(s)) for s in (65534, 65535, 0, 1)]
    assert all(g is not None and g[1] == 0 for g in got)
    assert r.counts["accepted"] == 4


def test_receiver_drops_duplicates_and_late_packets():
    r = Receiver(PCMU)
    for s in (10, 11, 12):
        assert r.handle(_pkt(s)) is not None
    assert r.handle(_pkt(12)) is None and r.handle(_pkt(11)) is None
    assert r.counts["duplicate_or_late"] == 2 and r.counts["accepted"] == 3


def test_receiver_reports_gaps_and_caps_long_ones():
    r = Receiver(PCMU)
    r.handle(_pkt(100))
    assert r.handle(_pkt(104))[1] == 480
    assert r.handle(_pkt(204))[1] == 8000
    assert r.counts["long_gaps"] == 1


def test_receiver_keeps_the_first_ssrc_and_refuses_a_change():
    r = Receiver(PCMU)
    assert r.handle(_pkt(1, ssrc=1)) is not None
    assert r.handle(_pkt(2, ssrc=2)) is None
    assert r.handle(_pkt(2, ssrc=1)) is not None
    assert r.counts["ssrc_changed"] == 1


def test_receiver_counts_parse_refusals_and_checks_its_payload_type():
    r = Receiver(PCMA)
    assert r.handle(_pkt(1, pt=PCMU)) is None and r.handle(b"short") is None
    assert r.counts["wrong_payload_type"] == 1 and r.counts["too_short"] == 1 and r.counts["accepted"] == 0
    with pytest.raises(ValueError):
        Receiver(96)


def test_counts_hold_no_packet_content():
    r = Receiver(PCMU)
    r.handle(_pkt(1, payload=b"secret-audio" * 10))
    assert all(isinstance(v, int) for v in r.counts.values())


def test_a_restarted_sequence_is_taken_up_after_two_consecutive_packets():
    r = Receiver(PCMU)
    r.handle(_pkt(5000))
    assert r.handle(_pkt(0)) is None and r.counts["jump_dropped"] == 1
    got = r.handle(_pkt(1))
    assert got is not None and got[1] == 0 and r.counts["resyncs"] == 1
    assert r.handle(_pkt(2)) is not None and r.handle(_pkt(2)) is None


def test_a_large_forward_jump_is_also_resynchronised_and_a_stray_packet_is_not():
    r = Receiver(PCMU)
    r.handle(_pkt(100))
    r.handle(_pkt(101))
    assert r.handle(_pkt(40000)) is None
    assert r.handle(_pkt(102)) is not None and r.counts["resyncs"] == 0
    assert r.handle(_pkt(20000)) is None and r.handle(_pkt(50000)) is None
    assert r.handle(_pkt(103)) is not None
    assert r.handle(_pkt(30000)) is None and r.handle(_pkt(30001)) is not None and r.counts["resyncs"] == 1


def test_the_gap_comes_from_the_timestamps():
    r = Receiver(PCMU)
    r.handle(_pkt(1))
    # consecutive sequence numbers but the timestamp jumps by 1000 samples (silence suppression): 1000 - 160 samples were not sent
    got = r.handle(build(PCMU, 2, 160 + 1000, 0x11223344, b"\xff" * 160))
    assert got[1] == 840 and r.counts["long_gaps"] == 0
    # a timestamp that goes backwards or stays put gives no gap
    assert r.handle(build(PCMU, 3, 5, 0x11223344, b"\xff" * 160))[1] == 0
    assert r.handle(build(PCMU, 4, 5, 0x11223344, b"\xff" * 160))[1] == 0


def test_the_timestamp_wraps_around():
    r = Receiver(PCMU)
    r.handle(build(PCMU, 1, 0xFFFFFF00, 7, b"\xff" * 160))
    assert r.handle(build(PCMU, 2, (0xFFFFFF00 + 160) & 0xFFFFFFFF, 7, b"\xff" * 160))[1] == 0
    assert r.handle(build(PCMU, 3, (0xFFFFFF00 + 320 + 480) & 0xFFFFFFFF, 7, b"\xff" * 160))[1] == 480


@pytest.mark.parametrize("kw", [dict(max_gap=0), dict(max_gap=-1), dict(max_gap=1.5), dict(max_gap=True), dict(max_gap="x"),
                                dict(max_payload=0), dict(max_payload=-5), dict(max_payload=2.5), dict(max_payload=True)])
def test_the_receiver_limits_are_checked(kw):
    with pytest.raises(ValueError):
        Receiver(PCMU, **kw)
