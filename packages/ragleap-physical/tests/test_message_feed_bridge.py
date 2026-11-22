"""The observe-only message feed adapter seen through the Bridge, and the names it borrows from the CSV adapter. Invented data only."""
import ast
from pathlib import Path

import pytest

from ragleap_physical import Bridge, Capability, CapabilityError, Envelope
from ragleap_physical.adapters import csv_replay, message_feed
from ragleap_physical.adapters.message_feed import FakeMessageFeedAdapter, FeedSpec, Topic

from helpers import Clock, cfg, propose

POWER = Topic("plant/power", "power", "W", 0.0, 5000.0)
MODE = Topic("plant/mode", "mode", "1", 0.0, 2.0, words={"idle": 0, "run": 1, "fault": 2})
FLOW = Topic("plant/flow", "flow", "kg", 0.0, 100.0, 0.5)
SPEC = FeedSpec([POWER, MODE, FLOW])


def msg(topic, t, payload):
    return '{"topic":"%s","time_s":%s,"payload":%s}' % (topic, t, payload)


GOOD = "\n".join([msg("plant/power", 0, 100), msg("plant/mode", 1, '"idle"'), msg("plant/power", 2.5, 120)]) + "\n"


def observe_bridge(adapter):
    return Bridge(adapter, Envelope(cfg()), org_id="org_demo", max_capability=Capability.OBSERVE, clock=Clock())


def test_the_bridge_accepts_a_replay_and_a_missing_topic_is_left_out_not_zero():
    a = FakeMessageFeedAdapter(GOOD, SPEC)
    b = observe_bridge(a)
    first = b.observe()
    assert first.dropped == 0 and not first.truncated and first.entities == ()
    got = {s.id: s for s in first.sensors}
    assert set(got) == {"power", "power_age_s", "topics_missing"}
    assert got["topics_missing"].value == 2.0 and got["topics_missing"].unit == "1"
    assert got["power"].value == 100.0 and got["power"].unit == "W" and got["power_age_s"].unit == "s"
    assert "mode" not in got and "flow" not in got  # absent, never shown as zero
    a.advance(5.0, 10)
    later = b.observe()
    assert later.dropped == 0
    values = {s.id: s.value for s in later.sensors}
    assert values["power"] == 120.0 and values["mode"] == 0.0 and values["topics_missing"] == 1.0 and "flow" not in values


def test_a_longest_sensor_id_keeps_its_age_sensor_through_the_bridge():
    sid = "s" * 48
    a = FakeMessageFeedAdapter(msg("plant/power", 0, 5) + "\n", FeedSpec([Topic("plant/power", sid, "W", 0.0, 10.0)]))
    seen = observe_bridge(a).observe()
    assert seen.dropped == 0 and {s.id for s in seen.sensors} == {sid, sid + "_age_s", "topics_missing"}


def test_the_largest_spec_fits_under_the_sensor_cap():
    topics = [Topic("t%d" % i, "s%d" % i, "1", 0.0, 100.0) for i in range(64)]
    text = "\n".join(msg("t%d" % i, i, 1) for i in range(64)) + "\n"
    a = FakeMessageFeedAdapter(text, FeedSpec(topics))
    assert a.advance(100.0, 100) and a.missing() == []  # every topic has had a message
    seen = observe_bridge(a).observe()
    assert seen.dropped == 0 and not seen.truncated and len(seen.sensors) == 64 * 2 + 1


def test_an_observe_bridge_never_proposes_a_command_to_a_replay():
    with pytest.raises(CapabilityError):
        propose(observe_bridge(FakeMessageFeedAdapter(GOOD, SPEC)))


def test_the_names_borrowed_from_the_csv_adapter_still_exist():
    tree = ast.parse(Path(message_feed.__file__).read_text(encoding="utf-8"))
    names = [a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level == 1 and n.module == "csv_replay" for a in n.names]
    assert {"CsvError", "_number", "_count", "_BAD_TEXT_RE", "_LONE_CR_RE"} <= set(names)
    for name in names:
        assert hasattr(csv_replay, name), name + " is gone from csv_replay.py"
