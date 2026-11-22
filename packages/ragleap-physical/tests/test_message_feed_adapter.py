"""An observe-only message feed adapter: strict parsing of untrusted recorded messages, replay as observations. Invented data only."""
import ast
import math
import os
import re
import tempfile
from pathlib import Path

import pytest

from ragleap_physical.adapters import message_feed
from ragleap_physical.adapters.csv_replay import CsvError
from ragleap_physical.adapters.message_feed import (FakeMessageFeedAdapter, FeedError, FeedSpec, Topic, parse_feed,
                                                    read_feed_file)

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixtures" / "synthetic_messages.jsonl"
DOC = HERE.parent / "docs" / "industry" / "message-feed-adapter.md"

# The hard caps, written out here and in the guide. The tests below use these numbers, not the module's constants, so a raised
# constant is caught instead of moving the test with it.
BYTES, MESSAGES, TOPICS, LINE, TOPIC, PAYLOAD, WORDS, WORD, SPAN = 5_000_000, 100_000, 64, 4096, 64, 32, 8, 16, 10_000_000

POWER = Topic("plant/power", "power", "W", 0.0, 5000.0)
MODE = Topic("plant/mode", "mode", "1", 0.0, 2.0, words={"idle": 0, "run": 1, "fault": 2})
FLOW = Topic("plant/flow", "flow", "kg", 0.0, 100.0, 0.5)
SPEC = FeedSpec([POWER, MODE, FLOW])


def msg(topic="plant/power", t=0, payload=1):
    return '{"topic":"%s","time_s":%s,"payload":%s}' % (topic, t, payload)


GOOD = "\n".join([msg("plant/power", 0, 100), msg("plant/mode", 1, '"idle"'), msg("plant/power", 2.5, 120)]) + "\n"


def close(a, b):
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)


def adapter(text=GOOD, spec=SPEC, **kw):
    return FakeMessageFeedAdapter(text, spec, **kw)


def values(a):
    return {s["id"]: s["value"] for s in a.observe_raw()["sensors"]}


def refuse(text, spec=SPEC, **kw):
    with pytest.raises(CsvError):
        parse_feed(text, spec, **kw)


# ---- what a good file gives
def test_values_are_scaled_words_are_numbers_and_time_is_relative_to_the_first_message():
    p = parse_feed("\n".join([msg("plant/flow", 10, 40), msg("plant/mode", 11, '"fault"'), msg("plant/power", 12.5, 7)]), SPEC)
    assert p.times == (0.0, 1.0, 2.5)
    assert p.values == (20.0, 2.0, 7.0)  # flow has scale 0.5; "fault" is 2
    assert p.topics == (2, 1, 0)


def test_the_replay_shows_the_last_value_per_topic_and_how_old_it_is():
    a = adapter()
    assert values(a) == {"power": 100.0, "power_age_s": 0.0, "topics_missing": 2.0}
    assert a.advance(1.5, 10) and a.n == 2
    v = values(a)
    assert v["power"] == 100.0 and close(v["power_age_s"], 1.5) and v["mode"] == 0.0 and close(v["mode_age_s"], 0.5)
    assert a.advance(5.0, 10) and a.n == 3
    v = values(a)
    assert v["power"] == 120.0 and close(v["power_age_s"], 2.5) and close(v["mode_age_s"], 4.0) and a.t == 5.0


def test_a_topic_with_no_message_is_missing_and_never_zero():
    a = adapter()
    ids = {s["id"] for s in a.observe_raw()["sensors"]}
    assert "flow" not in ids and "flow_age_s" not in ids and "mode" not in ids  # absent, not 0.0
    assert a.missing() == ["plant/mode", "plant/flow"]
    assert values(a)["topics_missing"] == 2.0
    a.advance(1.0, 10)
    assert a.missing() == ["plant/flow"] and values(a)["topics_missing"] == 1.0


def test_a_real_zero_is_a_value_and_is_not_missing():
    a = adapter(msg("plant/power", 0, 0) + "\n")
    assert values(a)["power"] == 0.0 and "power" in values(a) and a.missing() == ["plant/mode", "plant/flow"]


def test_max_steps_limits_the_messages_replayed_in_one_call_and_the_last_values_hold():
    a = adapter()
    assert a.advance(10.0, 1) is False and a.n == 2  # one message only, so the time was not reached
    assert a.advance(10.0, 5) is True and a.n == 3
    assert a.advance(10.0, 0) is True and a.n == 3


def test_replay_never_goes_back_in_time():
    a = adapter()
    a.advance(5.0, 10)
    a.advance(1.0, 10)
    assert a.t == 5.0 and a.n == 3


def test_the_same_data_gives_the_same_model_hash_and_other_data_does_not():
    assert adapter().observe_raw()["source"]["model_sha256"] == adapter().observe_raw()["source"]["model_sha256"]
    other = adapter(GOOD.replace("120", "121"))
    assert other.observe_raw()["source"]["model_sha256"] != adapter().observe_raw()["source"]["model_sha256"]
    assert adapter().observe_raw()["source"]["model_id"] == "message_feed"


def test_the_invented_fixture_replays_and_shows_one_topic_that_never_arrives():
    a = FakeMessageFeedAdapter(read_feed_file(FIXTURE), SPEC)
    assert a.advance(100.0, 100) and a.n == 5
    v = values(a)
    assert v["power"] == 2400.0 and v["mode"] == 1.0 and "flow" not in v and v["topics_missing"] == 1.0
    assert close(v["power_age_s"], 100.0 - 4.5) and close(v["mode_age_s"], 100.0 - 4.0)


# ---- observe only
def test_there_is_no_command_and_nothing_is_a_target():
    a = adapter()
    assert a.simulated is True and a.targets() == [] and a.observe_raw()["entities"] == []
    for call in (lambda: a.apply("power", "set_position_target", 1.0), lambda: a.dry_run("power", "set_position_target", 1.0, 1.0, 1)):
        with pytest.raises((ValueError, PermissionError, NotImplementedError)):
            call()
    assert a.n == 1 and a.t == 0.0  # nothing changed


def test_stop_and_clear_stop_never_raise_and_a_stopped_replay_does_not_move():
    a = adapter()
    a.stop()
    a.stop()
    assert a.advance(5.0, 10) is False and a.n == 1 and a.t == 0.0
    a.clear_stop()
    assert a.advance(5.0, 10) is True and a.n == 3


def test_a_stopped_replay_at_the_last_message_cannot_move_its_clock():
    a = adapter()
    a.advance(2.5, 10)
    assert a.n == 3 and a.t == 2.5
    a.stop()
    assert a.advance(9.0, 10) is False and a.t == 2.5


def test_advance_checks_its_arguments():
    a = adapter()
    for bad in (float("nan"), float("inf"), "1", None, True):
        with pytest.raises(ValueError):
            a.advance(bad, 1)
    for bad in (-1, 1.5, True, None, "1"):
        with pytest.raises(ValueError):
            a.advance(1.0, bad)
    with pytest.raises(ValueError):
        adapter(seed=True)
    with pytest.raises(ValueError):
        adapter(seed="1")


# ---- strict messages
def test_a_message_must_have_exactly_three_keys():
    refuse(GOOD + '{"topic":"plant/power","time_s":3}\n')
    refuse(GOOD + '{"topic":"plant/power","time_s":3,"payload":1,"extra":1}\n')
    refuse('{"topic":"plant/power","time_s":0,"payload":1,"payload":2}\n')  # repeated key, even with the same count of keys
    refuse('{"topic":"plant/power","time_s":0,"payload":1,"topic":"plant/mode"}\n')


def test_a_repeated_key_is_refused_even_when_three_distinct_keys_remain():
    refuse('{"topic":"plant/power","topic":"plant/power","time_s":0,"payload":1}\n')


def test_each_line_is_one_json_object_and_nothing_else():
    for text in ("[]\n", "1\n", '"x"\n', "null\n", "{\n", "{} \n", '{"topic":"plant/power","time_s":0,"payload":1} x\n',
                 "{'topic':'plant/power','time_s':0,'payload':1}\n"):
        refuse(text)
    refuse(msg() + "," + msg() + "\n")


def test_not_a_number_and_infinity_are_refused_in_time_and_payload():
    for word in ("NaN", "Infinity", "-Infinity"):
        refuse(msg(payload=word) + "\n")
        refuse(msg(t=word) + "\n")
    refuse(msg(payload="1e999") + "\n")  # overflows to infinity
    refuse(msg(t="1e999") + "\n")


def test_payload_and_time_types_are_checked():
    for payload in ("true", "false", "null", "[]", "[1]", "{}", '{"a":1}', '""', '"1"', '"Idle"', '"unknown"'):
        refuse(msg("plant/mode", 0, payload) + "\n")
    refuse(msg(t="true") + "\n")
    refuse(msg(t='"1"') + "\n")
    refuse(msg(t="null") + "\n")
    refuse(msg(t="[0]") + "\n")


def test_a_word_is_refused_for_a_topic_that_declares_none_and_a_number_is_fine_for_a_word_topic():
    refuse(msg("plant/power", 0, '"idle"') + "\n")
    assert parse_feed(msg("plant/mode", 0, 1) + "\n", SPEC).values == (1.0,)


def test_an_undeclared_topic_is_refused_and_so_is_a_topic_of_the_wrong_type():
    refuse(msg("plant/other", 0, 1) + "\n")
    refuse(msg("Plant/Power", 0, 1) + "\n")
    refuse(msg("plant/power ", 0, 1) + "\n")
    refuse('{"topic":1,"time_s":0,"payload":1}\n')
    refuse('{"topic":null,"time_s":0,"payload":1}\n')
    refuse('{"topic":["plant/power"],"time_s":0,"payload":1}\n')


def test_escaped_characters_cannot_smuggle_a_topic_or_a_word():
    refuse('{"topic":"plant\\u002fpower\\u0000","time_s":0,"payload":1}\n')
    refuse('{"topic":"plant/mode","time_s":0,"payload":"idl\\u0065\\u0000"}\n')
    refuse('{"topic":"plant/mode","time_s":0,"payload":"id\\ule"}\n')


def test_values_must_lie_in_the_declared_range_after_scaling():
    refuse(msg("plant/power", 0, -0.001) + "\n")
    refuse(msg("plant/power", 0, 5000.001) + "\n")
    refuse(msg("plant/flow", 0, 200.001) + "\n")  # scaled by 0.5, so 100.0005
    assert parse_feed(msg("plant/power", 0, 5000) + "\n" + msg("plant/flow", 1, 200) + "\n", SPEC).values == (5000.0, 100.0)
    assert parse_feed(msg("plant/power", 0, 0) + "\n", SPEC).values == (0.0,)


def test_time_must_strictly_increase_across_all_topics():
    refuse(msg("plant/power", 1, 1) + "\n" + msg("plant/mode", 1, '"idle"') + "\n")  # equal
    refuse(msg("plant/power", 2, 1) + "\n" + msg("plant/mode", 1, '"idle"') + "\n")  # back
    refuse(msg("plant/power", 1, 1) + "\n" + msg("plant/power", 1, 1) + "\n")  # a repeated message at the same time


def test_time_may_start_anywhere_and_is_shown_relative():
    assert parse_feed(msg(t=-5) + "\n" + msg(t=-4.5) + "\n", SPEC).times == (0.0, 0.5)
    refuse(msg(t=1.0e12 + 1) + "\n")
    refuse(msg(t=-1.0e12 - 1) + "\n")


# ---- text
def test_only_printable_ascii_and_proper_line_ends_are_accepted():
    refuse(msg() + "\n\n" + msg(t=1) + "\n")  # blank line
    refuse("\n" + msg() + "\n")
    refuse("")
    refuse("\n")
    refuse(msg().replace("plant", "pl\u00e4nt") + "\n")
    refuse(msg() + "\t\n")
    refuse(msg() + "\x00\n")
    refuse(msg() + "\r" + msg(t=1) + "\n")  # lone carriage return
    refuse(b"\xff\xfe" + msg().encode() + b"\n")
    refuse(12)
    refuse(None)
    assert parse_feed((msg() + "\r\n" + msg(t=1) + "\r\n"), SPEC).times == (0.0, 1.0)
    assert parse_feed(msg(), SPEC).times == (0.0,)  # no final line end
    assert parse_feed(b"\xef\xbb\xbf" + msg().encode(), SPEC).times == (0.0,)  # one byte order mark
    assert parse_feed(bytearray(msg().encode()), SPEC).times == (0.0,)


def test_a_json_nest_that_is_too_deep_is_refused_cleanly():
    deep = "[" * 2000 + "]" * 2000
    refuse('{"topic":"plant/power","time_s":0,"payload":%s}\n' % deep)
    refuse("[" * 3000 + "\n")


# ---- caps, written as absolute numbers
def test_the_hard_caps_are_these_numbers():
    assert (message_feed.MAX_MESSAGES, message_feed.MAX_TOPICS, message_feed.MAX_TOPIC_CHARS) == (MESSAGES, TOPICS, TOPIC)
    assert (message_feed.MAX_PAYLOAD_CHARS, message_feed.MAX_WORDS, message_feed.MAX_WORD_CHARS) == (PAYLOAD, WORDS, WORD)
    assert (message_feed.MAX_BYTES, message_feed.MAX_LINE_CHARS, message_feed.MAX_SPAN_S) == (BYTES, LINE, SPAN)


def test_the_caller_may_ask_for_less_but_never_for_more():
    refuse(GOOD, max_messages=2)
    refuse(GOOD, max_bytes=len(GOOD) - 1)
    refuse(GOOD.encode(), max_bytes=len(GOOD) - 1)  # bytes and text have their own size check
    parse_feed(GOOD.encode(), SPEC, max_bytes=len(GOOD))
    parse_feed(GOOD, SPEC, max_messages=3, max_bytes=len(GOOD))
    for kw in ({"max_messages": MESSAGES + 1}, {"max_bytes": BYTES + 1}, {"max_messages": 0}, {"max_bytes": 0},
               {"max_messages": True}, {"max_bytes": 1.5}):
        refuse(GOOD, **kw)
        with pytest.raises(CsvError):
            adapter(**kw)


def test_data_above_the_byte_cap_is_refused_for_bytes_and_for_text():
    refuse(b" " * (BYTES + 1))
    refuse(" " * (BYTES + 1))
    refuse("\u00e4" * (BYTES // 2 + 1))  # more than the cap once encoded


def test_a_line_above_the_line_cap_is_refused_and_one_at_the_cap_is_not():
    base = msg()
    pad = LINE - len(base)
    assert parse_feed(base[:-1] + " " * pad + "}\n", SPEC).values == (1.0,)  # exactly LINE characters
    refuse(base[:-1] + " " * (pad + 1) + "}\n")


def test_a_number_text_above_the_payload_cap_is_refused_and_one_at_the_cap_is_not():
    ok_text = "1." + "0" * (PAYLOAD - 2)
    assert len(ok_text) == PAYLOAD
    assert parse_feed(msg(payload=ok_text) + "\n", SPEC).values == (1.0,)
    refuse(msg(payload=ok_text + "0") + "\n")
    refuse(msg(payload="1" * (PAYLOAD + 1)) + "\n")
    refuse(msg(t="1." + "0" * PAYLOAD) + "\n")  # time is a number too


def test_an_integer_text_above_the_payload_cap_is_refused_even_when_its_scaled_value_is_in_range():
    spec = FeedSpec([Topic("t", "t", "1", 0.0, 5000.0, 1.0e-30)])
    assert close(parse_feed(msg("t", 0, "1" + "0" * (PAYLOAD - 1)) + "\n", spec).values[0], 1.0e1)  # 32 characters, 1e31 * 1e-30
    refuse(msg("t", 0, "1" + "0" * PAYLOAD) + "\n", spec)  # 33 characters, value 1e32 * 1e-30 = 100 is in range


def test_a_word_above_the_word_cap_is_refused_in_the_payload_and_in_the_declaration():
    word = "w" * WORD
    spec = FeedSpec([Topic("t", "t", "1", 0.0, 1.0, words={word: 1})])
    assert parse_feed(msg("t", 0, '"%s"' % word) + "\n", spec).values == (1.0,)
    refuse(msg("t", 0, '"%s"' % (word + "w")) + "\n", spec)
    with pytest.raises(CsvError):
        Topic("t", "t", "1", 0.0, 1.0, words={word + "w": 1})
    many = {"w%d" % i: 0 for i in range(WORDS)}
    Topic("t", "t", "1", 0.0, 1.0, words=many)
    with pytest.raises(CsvError):
        Topic("t", "t", "1", 0.0, 1.0, words=dict(many, extra=0))


def test_a_topic_above_the_topic_cap_cannot_be_declared_and_one_at_the_cap_can():
    Topic("a" * TOPIC, "a", "1", 0.0, 1.0)
    with pytest.raises(CsvError):
        Topic("a" * (TOPIC + 1), "a", "1", 0.0, 1.0)
    refuse(msg("a" * (TOPIC + 1), 0, 1) + "\n")


def test_the_number_of_topics_is_capped():
    many = [Topic("t%d" % i, "s%d" % i, "1", 0.0, 1.0) for i in range(TOPICS)]
    FeedSpec(many)
    with pytest.raises(CsvError):
        FeedSpec(many + [Topic("extra", "extra", "1", 0.0, 1.0)])
    with pytest.raises(CsvError):
        FeedSpec([])


def test_the_time_span_is_capped_and_a_span_at_the_cap_is_not():
    assert parse_feed(msg(t=0) + "\n" + msg(t=SPAN) + "\n", SPEC).times == (0.0, 10_000_000.0)
    refuse(msg(t=0) + "\n" + msg(t=SPAN + 1) + "\n")


def test_the_message_cap_counts_lines():
    text = "\n".join(msg(t=i) for i in range(MESSAGES + 1)) + "\n"
    refuse(text, max_bytes=BYTES)  # the byte cap or the message cap refuses it; either way it is not replayed
    lines = [msg(t=i) for i in range(5)]
    parse_feed("\n".join(lines), SPEC, max_messages=5)
    refuse("\n".join(lines), SPEC, max_messages=4)


# ---- declarations
def test_declarations_are_checked():
    ok = dict(topic="t", sensor_id="s", unit="1", minimum=0.0, maximum=1.0)
    Topic(**ok)
    for change in ({"topic": ""}, {"topic": 1}, {"topic": "a b"}, {"topic": "a\n"}, {"sensor_id": "bad id"}, {"sensor_id": "data_age_s"},
                   {"sensor_id": "topics_missing"}, {"sensor_id": "x_age_s"}, {"sensor_id": "s" * 49}, {"unit": "degC"},
                   {"unit": 1}, {"minimum": 1.0}, {"minimum": float("nan")}, {"maximum": float("inf")}, {"minimum": True},
                   {"scale": 0.0}, {"scale": 1.0e13}, {"scale": float("nan")}, {"scale": "1"}, {"words": {"1x": 0}},
                   {"words": {"x": 2}}, {"words": {"x": float("nan")}}, {"words": {"x": True}}, {"words": ["x"]}):
        with pytest.raises(CsvError):
            Topic(**dict(ok, **change))
    Topic(**dict(ok, sensor_id="s" * 48))
    Topic(**dict(ok, scale=1.0e12))
    with pytest.raises(CsvError):
        FeedSpec([Topic(**ok), Topic(**dict(ok, sensor_id="s2"))])  # same topic twice
    with pytest.raises(CsvError):
        FeedSpec([Topic(**ok), Topic(**dict(ok, topic="u"))])  # same sensor id twice
    with pytest.raises(CsvError):
        FeedSpec("not a list")
    with pytest.raises(CsvError):
        FeedSpec([object()])
    with pytest.raises(CsvError):
        parse_feed(GOOD, "not a spec")


# ---- errors never repeat text from the file
def test_an_error_never_repeats_text_from_the_file():
    marker = "ZZMARKERZZ"
    bad = [
        '{"topic":"%s","time_s":0,"payload":1}\n' % marker,
        '{"topic":"plant/mode","time_s":0,"payload":"%s"}\n' % marker,
        '{"topic":"plant/power","time_s":0,"payload":1,"%s":2}\n' % marker,
        '{"topic":"plant/power","time_s":0,"payload":1,"%s":2,"%s":3}\n' % (marker, marker),
        '{"%s":1,"%s":2,"topic":"plant/power","time_s":0,"payload":1}\n' % (marker, marker),
        '{"topic":"plant/power","time_s":"%s","payload":1}\n' % marker,
        '{"topic":"plant/power","time_s":0,"payload":"%s"}\n' % marker,
        "%s\n" % marker,
        '{"topic":"plant/power","time_s":0,"payload":1} %s\n' % marker,
        '{"topic":"plant/power","time_s":0,"payload":1}\n{"topic":"%s","time_s":1,"payload":1}\n' % marker,
    ]
    for text in bad:
        with pytest.raises(CsvError) as info:
            parse_feed(text, SPEC)
        assert marker not in str(info.value)
    with pytest.raises(CsvError) as info:
        parse_feed(msg("plant/power", 0, 9999) + "\n", SPEC)
    assert "line 1" in str(info.value) and "power" in str(info.value) and "9999" not in str(info.value)


def test_an_error_names_the_line():
    with pytest.raises(CsvError) as info:
        parse_feed(msg(t=0) + "\n" + msg(t=1) + "\nnot json\n", SPEC)
    assert "line 3" in str(info.value)


# ---- reading a file
def test_read_feed_file_reads_a_regular_file_with_a_cap():
    data = read_feed_file(FIXTURE)
    assert data.startswith(b'{"topic"') and len(data) == FIXTURE.stat().st_size
    with pytest.raises(CsvError):
        read_feed_file(FIXTURE, max_bytes=10)
    with pytest.raises(CsvError):
        read_feed_file(FIXTURE, max_bytes=BYTES + 1)
    with pytest.raises(CsvError):
        read_feed_file(HERE)  # a directory
    with pytest.raises(CsvError):
        read_feed_file(HERE / "no_such_file.jsonl")


def test_read_feed_file_refuses_a_symbolic_link():
    with tempfile.TemporaryDirectory() as d:
        link = os.path.join(d, "link.jsonl")
        try:
            os.symlink(str(FIXTURE), link)
        except (OSError, NotImplementedError):
            pytest.skip("symbolic links are not available here")
        if not hasattr(os, "O_NOFOLLOW"):
            pytest.skip("this platform cannot refuse links when opening")
        with pytest.raises(CsvError):
            read_feed_file(link)


# ---- the module itself
def test_the_module_imports_only_the_standard_library_it_needs_and_has_no_network_code():
    src = Path(message_feed.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    absolute, relative = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            absolute.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            (relative if node.level else absolute).add((node.module or "").split(".")[0])
    assert absolute <= {"__future__", "hashlib", "json", "math", "re", "dataclasses", "typing"}, absolute
    assert relative <= {"observation", "base", "csv_replay"}, relative
    assert "socket" not in src and "urllib" not in src and "subprocess" not in src
    assert not re.search(r"\bopen\(", src)  # the file is read by read_csv_file, with its own checks


def test_the_guide_lists_every_hard_cap_with_its_number():
    text = DOC.read_text(encoding="utf-8")
    rows = {}
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.startswith("|") and len(cells) == 2:
            rows[cells[0].lower()] = cells[1]
    want = {"bytes": "5,000,000", "messages": "100,000", "topics": "64", "characters per line": "4,096",
            "characters per topic": "64", "characters per payload number": "32", "declared words per topic": "8",
            "characters per declared word": "16", "time span in seconds": "10,000,000"}
    for key, number in want.items():
        assert rows.get(key) == number, key
