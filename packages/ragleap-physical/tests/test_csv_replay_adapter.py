"""An observe-only CSV replay adapter: strict parsing of untrusted text, replay as observations. Invented data only."""
import ast
import math
import os
import re
import tempfile
import threading
from pathlib import Path

import pytest

from ragleap_physical import Bridge, Capability, CapabilityError, Envelope, PhysicalError
from ragleap_physical.adapters import csv_replay
from ragleap_physical.adapters.csv_replay import (ALLOWED_UNITS, MAX_BYTES, MAX_COLUMNS, MAX_FIELD_CHARS, MAX_LINE_CHARS, MAX_ROWS,
                                                  MAX_SCALE, MAX_SPAN_S, MILLING_MACHINE_SIGNALS, Column, CsvError, CsvSpec,
                                                  FakeCsvReplayAdapter, parse_csv, read_csv_file)
from ragleap_physical.observation import UNITS

from helpers import Clock, cfg, propose

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixtures" / "synthetic_signals.csv"
DOC = HERE.parent / "docs" / "industry" / "csv-replay-adapter.md"

# The hard caps, written out here and in the guide. The tests below use these numbers, not the module's constants, so a raised
# constant is caught instead of moving the test with it.
BYTES, ROWS, COLUMNS, LINE, FIELD, SPAN = 5_000_000, 100_000, 64, 4096, 32, 10_000_000

SPEC = CsvSpec("t_ms", [Column("temp", "temp", "1", 0.0, 100.0), Column("speed", "speed", "1", -1000.0, 1000.0, 0.5)],
               time_scale=0.001, ignore=["note"])
GOOD = "t_ms,temp,speed,note\n0,20.5,100,a\n500,20.6,110,b\n1500,21.0,120,c\n"


def close(a, b):
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)


def adapter(text=GOOD, spec=SPEC, **kw):
    return FakeCsvReplayAdapter(text, spec, **kw)


def values(a):
    return {s["id"]: s["value"] for s in a.observe_raw()["sensors"]}


def refuse(text, spec=SPEC, **kw):
    with pytest.raises(CsvError):
        parse_csv(text, spec, **kw)


def row(temp="1", speed="1", t="0", note="a"):
    return "t_ms,temp,speed,note\n%s,%s,%s,%s\n" % (t, temp, speed, note)


# ---- what a good file gives
def test_values_are_scaled_and_time_is_relative_to_the_first_row():
    p = parse_csv(GOOD, SPEC)
    assert p.times == (0.0, 0.5, 1.5)
    assert p.rows[0] == (20.5, 50.0) and p.rows[2] == (21.0, 60.0)  # speed has scale 0.5


def test_the_replay_shows_the_last_row_reached_and_how_old_it_is():
    a = adapter()
    assert values(a) == {"temp": 20.5, "speed": 50.0, "data_age_s": 0.0}
    assert a.advance(0.7, 10) and a.idx == 1 and close(values(a)["data_age_s"], 0.2) and values(a)["temp"] == 20.6
    assert a.advance(5.0, 10) and a.idx == 2 and close(values(a)["data_age_s"], 3.5) and a.t == 5.0


def test_max_steps_limits_the_rows_replayed_in_one_call_and_the_last_values_hold():
    a = adapter()
    assert a.advance(10.0, 1) is False and a.idx == 1  # one row only, so the time was not reached
    assert a.advance(10.0, 5) is True and a.idx == 2
    assert a.advance(10.0, 0) is True and a.idx == 2


def test_the_same_inputs_give_the_same_model_hash_and_other_inputs_do_not():
    sha = lambda a: a.observe_raw()["source"]["model_sha256"]
    assert sha(adapter()) == sha(adapter()) == sha(adapter(seed=7))
    assert sha(adapter()) != sha(adapter(GOOD.replace("20.6", "20.7")))
    other = CsvSpec("t_ms", [Column("temp", "temp", "1", 0.0, 100.0), Column("speed", "speed", "1", -1000.0, 1000.0, 0.25)],
                    time_scale=0.001, ignore=["note"])
    assert sha(adapter()) != sha(adapter(spec=other))
    more_ignored = CsvSpec("t_ms", SPEC.columns, time_scale=0.001, ignore=["note", "spare"])
    assert sha(adapter()) != sha(adapter(spec=more_ignored))  # what the description allows is part of the model


def test_crlf_and_one_leading_byte_order_mark_are_accepted_and_a_second_is_not():
    assert parse_csv(GOOD.replace("\n", "\r\n"), SPEC).times == (0.0, 0.5, 1.5)
    assert parse_csv(b"\xef\xbb\xbf" + GOOD.encode(), SPEC).times == (0.0, 0.5, 1.5)
    refuse(b"\xef\xbb\xbf\xef\xbb\xbf" + GOOD.encode())


def test_plain_numbers_are_accepted():
    for good in ("-0", "0.0", "5", "1E1", "1e-3", "99.999999"):
        parse_csv(row(temp=good), SPEC)


# ---- observe only
def test_there_is_no_command_and_nothing_is_a_target():
    a = adapter()
    assert a.simulated is True and a.targets() == []
    for call in (lambda: a.apply("t", "set_position_target", 1.0), lambda: a.dry_run("t", "set_position_target", 1.0, 10.0, 100)):
        with pytest.raises(ValueError):
            call()
    assert a.t == 0.0 and a.idx == 0


def test_an_observe_bridge_reads_but_never_proposes_and_the_core_accepts_the_observation():
    a = adapter()
    b = Bridge(a, Envelope(cfg()), org_id="org_demo", max_capability=Capability.OBSERVE, clock=Clock())
    first = b.observe()
    assert first.dropped == 0 and len(first.sensors) == 3 and not first.truncated
    a.advance(1.0, 10)
    assert {s.id: s.value for s in b.observe().sensors}["temp"] == 20.6
    with pytest.raises(CapabilityError):
        propose(b)


def test_even_a_control_bridge_cannot_change_the_replay():
    a = adapter()
    b = Bridge(a, Envelope(cfg()), org_id="org_demo", max_capability=Capability.CONTROL, clock=Clock())
    with pytest.raises((PhysicalError, ValueError)):  # fail closed at whichever step first refuses
        p = propose(b)
        b.approve(p, "person:alice")
        b.apply(p)
    assert a.t == 0.0 and a.idx == 0


def test_stop_never_raises_holds_the_replay_and_clear_stop_lets_it_go_on():
    a = adapter()
    entities = len(a.observe_raw()["entities"])
    a.stop()
    assert a.advance(10.0, 10) is False and a.idx == 0 and len(a.observe_raw()["entities"]) == entities
    a.clear_stop()
    assert a.advance(10.0, 10) is True and a.idx == 2
    a.clear_stop()  # clearing when nothing is stopped is fine
    a.stop()  # at the last row, where there is nothing to step to, a stopped replay still does not let its time run on
    assert a.advance(20.0, 10) is False and a.t == 10.0
    a.clear_stop()
    assert a.advance(20.0, 10) is True and a.t == 20.0
    b = Bridge(adapter(), Envelope(cfg()), org_id="org_demo", max_capability=Capability.OBSERVE, clock=Clock())
    b.stop()
    b.clear_stop()


def test_advance_refuses_values_that_are_not_a_finite_time_or_a_step_count():
    a = adapter()
    for to in (float("nan"), float("inf"), True, "1", None):
        with pytest.raises(ValueError):
            a.advance(to, 10)
    for steps in (-1, True, 1.5, "1", None):
        with pytest.raises(ValueError):
            a.advance(1.0, steps)
    assert a.idx == 0


# ---- the caps
def test_the_hard_caps_are_the_ones_the_guide_states():
    assert (MAX_BYTES, MAX_ROWS, MAX_COLUMNS, MAX_LINE_CHARS, MAX_FIELD_CHARS, MAX_SPAN_S) == (BYTES, ROWS, COLUMNS, LINE, FIELD, float(SPAN))
    assert MAX_SCALE == 1.0e12
    guide = DOC.read_text(encoding="utf-8")
    for text in ("5,000,000 bytes", "100,000 data rows", "64 header cells", "4,096 characters", "at most 32", "10,000,000 seconds"):
        assert text in guide, text


def test_the_byte_cap_holds_for_bytes_and_for_text_and_cannot_be_raised_above_the_hard_cap():
    refuse(GOOD, max_bytes=len(GOOD) - 1)
    refuse(GOOD.encode(), max_bytes=len(GOOD) - 1)
    parse_csv(GOOD.encode(), SPEC, max_bytes=len(GOOD))
    for bad in (BYTES + 1, 0, -1, True, 1.5, "10", None):
        refuse(GOOD, max_bytes=bad)
    refuse(row() + "#" * (BYTES + 1))  # a text above the hard cap is refused before any parsing
    refuse((row() + "#" * (BYTES + 1)).encode())


def test_the_row_cap_holds_and_cannot_be_raised_above_the_hard_cap():
    three = GOOD
    parse_csv(three, SPEC, max_rows=3)
    refuse(three, max_rows=2)
    for bad in (ROWS + 1, 0, True, 2.5, None):
        refuse(three, max_rows=bad)
    refuse("t_ms,temp,speed,note\n")  # a header and no data
    refuse("")


def test_the_column_cap_and_the_line_cap():
    names = ["n%02d" % i + "x" * 61 for i in range(COLUMNS - 1)]  # 64 characters each
    columns = [Column(n, "s%d" % i, "1", 0.0, 10.0) for i, n in enumerate(names)]
    spec = CsvSpec("t" + "y" * 63, columns, ignore=[])
    long_header = ",".join(["t" + "y" * 63] + names)
    assert len(long_header.split(",")) == COLUMNS and len(long_header) > LINE
    refuse(long_header + "\n" + ",".join(["0"] * COLUMNS) + "\n", spec)  # the line is too long even though every name is declared
    short = [Column("c%d" % i, "s%d" % i, "1", 0.0, 10.0) for i in range(COLUMNS - 1)]
    spec = CsvSpec("t", short, ignore=[])
    header = ",".join(["t"] + ["c%d" % i for i in range(COLUMNS - 1)])
    p = parse_csv(header + "\n" + ",".join(["0"] * COLUMNS) + "\n", spec)  # 64 header cells are the most
    assert len(p.rows[0]) == COLUMNS - 1
    with pytest.raises(CsvError):  # 64 sensors and a time column are too many for a description
        CsvSpec("t", short + [Column("extra", "extra", "1", 0.0, 1.0)], ignore=[])


def test_the_field_cap():
    parse_csv(row(temp="0." + "0" * (FIELD - 2)), SPEC)  # exactly 32 characters
    parse_csv(row(note="a" * FIELD), SPEC)
    refuse(row(temp="0." + "0" * (FIELD - 1)))  # 33 characters of a perfectly plain number
    refuse(row(note="a" * (FIELD + 1)))  # also in an ignored column


def test_the_time_span_cap():
    spec = CsvSpec("t", [Column("v", "v", "1", 0.0, 1.0)])
    parse_csv("t,v\n0,0\n%d,0\n" % SPAN, spec)
    refuse("t,v\n0,0\n%d,0\n" % (SPAN + 1), spec)
    refuse("t,v\n5,0\n%d,0\n" % (SPAN + 6), spec)  # the span counts from the first row, not from zero
    parse_csv("t,v\n5,0\n%d,0\n" % (SPAN + 5), spec)


# ---- text, header and rows
def test_text_that_is_not_plain_printable_ascii_is_refused():
    refuse(b"\xff\xfe" + GOOD.encode())  # not UTF-8
    refuse(GOOD.replace("a\n", "\xe9\n", 1))  # a non-ASCII letter, even in an ignored column
    refuse(GOOD.replace("a\n", "a\x00\n", 1))
    refuse(GOOD.replace("a\n", "a\t\n", 1))
    refuse(GOOD.replace("a\n", "a\x1b[31m\n", 1))
    refuse(GOOD.replace("a\n", "a\rb\n", 1))  # a carriage return that is not followed by a line feed, in a cell that is not parsed
    refuse(GOOD.replace("temp", "te mp", 1))
    for bad in (12, None, 1.5, ["a,b"]):
        refuse(bad)


def test_quotes_blank_lines_and_ragged_rows_are_refused():
    refuse(GOOD.replace("20.5", '"20.5"'))
    refuse(GOOD.replace("a\n", '"a"\n', 1))  # quotes in a cell that is not parsed
    refuse(GOOD + "\n")  # a blank line after the data
    refuse(GOOD.replace("\n500", "\n\n500"))
    refuse("\n" + GOOD)
    refuse(GOOD.replace("0,20.5,100,a", "0,20.5,100"))
    refuse(GOOD.replace("0,20.5,100,a", "0,20.5,100,a,b"))
    refuse(row(temp=""))  # an empty cell
    refuse(row(t=""))


def test_the_header_must_match_the_declaration_exactly():
    refuse(GOOD.replace("temp,speed", "temp,temp"))  # repeated
    refuse("t_ms,temp,speed,note,temp\n0,1,1,a,1\n")  # every declared column is there, one of them twice
    refuse(GOOD.replace("note", "extra"))  # neither declared nor ignored on purpose
    refuse("t_ms,temp,speed,note,other\n0,1,1,a,1\n")  # one more column than declared
    refuse(GOOD.replace("speed", "spd"))  # a declared column is missing and another is unknown
    refuse(GOOD.replace("t_ms", "T_MS"))  # names are case sensitive
    spec = CsvSpec("t_ms", SPEC.columns, time_scale=0.001, ignore=["note", "spare"])
    parse_csv(GOOD, spec)  # an ignored column may be absent
    refuse("temp,speed,note\n1,1,a\n")  # the time column is missing
    refuse("t_ms,note\n1,a\n")  # every declared column is missing


def test_numbers_must_be_plain_decimals():
    for bad in ("nan", "NaN", "inf", "-inf", "Infinity", "1_0", "+1", " 1", "1 ", "0x10", "1e", "1.", ".5", "--1", "1e+", "1,5", "٣", "1\n"):
        with pytest.raises(CsvError):
            parse_csv(row(temp=bad), SPEC)
    refuse(row(temp="1e999"))  # a plain number that is too large for a float
    refuse(row(temp="-1e999"))
    refuse(row(t="nan"))
    refuse(row(t="1e999"))
    refuse(row(t="abc"))


def test_every_value_must_lie_in_its_declared_range_after_scaling():
    parse_csv(row(temp="0"), SPEC)
    parse_csv(row(temp="100"), SPEC)
    refuse(row(temp="-0.001"))
    refuse(row(temp="100.001"))
    parse_csv(row(speed="2000"), SPEC)  # scaled by 0.5 this is 1000, the declared maximum
    parse_csv(row(speed="-2000"), SPEC)
    refuse(row(speed="2000.1"))
    refuse(row(speed="-2000.1"))


def test_time_must_strictly_increase():
    refuse("t_ms,temp,speed,note\n10,1,1,a\n5,1,1,a\n")
    refuse("t_ms,temp,speed,note\n10,1,1,a\n10,1,1,a\n")
    parse_csv("t_ms,temp,speed,note\n10,1,1,a\n11,1,1,a\n", SPEC)
    parse_csv("t_ms,temp,speed,note\n-5,1,1,a\n-4,1,1,a\n", SPEC)  # only the differences matter
    times = parse_csv("t_ms,temp,speed,note\n10,1,1,a\n11,1,1,a\n", SPEC).times  # relative to the first row
    assert times[0] == 0.0 and close(times[1], 0.001)


def test_an_error_message_never_repeats_text_from_the_file():
    marker = "ZQXJMARKER"
    for text in (GOOD.replace("note", marker), row(temp=marker), row(note=marker * 20), GOOD.replace("t_ms", marker), row(t=marker)):
        with pytest.raises(CsvError) as info:
            parse_csv(text, SPEC)
        assert marker not in str(info.value)


# ---- the description is checked too
def test_a_column_description_must_be_valid():
    ok = dict(name="a", sensor_id="a", unit="1", minimum=0.0, maximum=1.0)
    Column(**ok)
    for change in ({"unit": "mm"}, {"unit": ""}, {"unit": None}, {"unit": "M"}, {"scale": 0.0}, {"scale": float("nan")},
                   {"scale": float("inf")}, {"scale": True}, {"scale": "1"}, {"scale": 1e13}, {"minimum": 1.0}, {"minimum": 2.0},
                   {"minimum": float("nan")}, {"maximum": float("inf")}, {"maximum": True}, {"minimum": "0"},
                   {"sensor_id": "a b"}, {"sensor_id": "a\n"}, {"sensor_id": ""}, {"sensor_id": "x" * 65}, {"sensor_id": "data_age_s"},
                   {"sensor_id": 5}, {"name": "a b"}, {"name": ""}, {"name": "a\n"}, {"name": "x" * 65}, {"name": None}):
        with pytest.raises(CsvError):
            Column(**dict(ok, **change))
    assert ALLOWED_UNITS == set(UNITS)
    assert Column(**dict(ok, scale=1)).scale == 1.0 and isinstance(Column(**dict(ok, scale=1)).scale, float)
    Column(**dict(ok, scale=1e12))
    Column(**dict(ok, scale=-1e12))


def test_a_file_description_must_be_valid():
    col = Column("a", "a", "1", 0.0, 1.0)
    CsvSpec("t", [col])
    other = Column("b", "b", "1", 0.0, 1.0)
    for args in (dict(time_column="t b", columns=[col]), dict(time_column="", columns=[col]), dict(time_column=None, columns=[col]),
                 dict(time_column="t", columns=[]), dict(time_column="t", columns="a"), dict(time_column="t", columns=[col, col]),
                 dict(time_column="t", columns=[col, Column("b", "a", "1", 0.0, 1.0)]),  # a sensor id used twice
                 dict(time_column="t", columns=["a"]), dict(time_column="a", columns=[col]),  # the time column is also a sensor
                 dict(time_column="t", columns=[col], ignore="b"), dict(time_column="t", columns=[col], ignore=["a"]),
                 dict(time_column="t", columns=[col], ignore=["t"]), dict(time_column="t", columns=[col], ignore=["x", "x"]),
                 dict(time_column="t", columns=[col], ignore=["b c"]), dict(time_column="t", columns=[col], ignore=[5]),
                 dict(time_column="t", columns=[col], time_scale=0.0), dict(time_column="t", columns=[col], time_scale=-1.0),
                 dict(time_column="t", columns=[col], time_scale=float("nan")), dict(time_column="t", columns=[col], time_scale=2e6),
                 dict(time_column="t", columns=[col], time_scale=True), dict(time_column="t", columns=[col, other], ignore=["b"])):
        with pytest.raises(CsvError):
            CsvSpec(**args)
    with pytest.raises(CsvError):
        parse_csv(GOOD, "not a spec")


def test_the_adapter_refuses_a_bad_seed_and_a_bad_cap():
    for seed in (True, "1", 1.5, None):
        with pytest.raises(ValueError):
            adapter(seed=seed)
    for kw in ({"max_bytes": 0}, {"max_bytes": BYTES + 1}, {"max_rows": 0}, {"max_rows": ROWS + 1}, {"max_rows": True}):
        with pytest.raises(ValueError):
            adapter(**kw)


# ---- reading a local file
def test_the_synthetic_fixture_loads_through_the_milling_profile():
    data = read_csv_file(FIXTURE)
    assert data == FIXTURE.read_bytes()
    a = FakeCsvReplayAdapter(data, MILLING_MACHINE_SIGNALS, seed=3)
    first = values(a)
    assert set(first) == {"pos_dev_x", "pos_dev_y", "tool_x", "tool_y", "tool_z", "spindle_torque", "data_age_s"}
    assert close(first["tool_x"], 0.120) and close(first["tool_z"], 0.025) and close(first["spindle_torque"], 4.0)  # millimetres became metres
    assert {s["id"]: s["unit"] for s in a.observe_raw()["sensors"]}["tool_x"] == "m"
    assert a.advance(0.004, 100) and a.idx == 2 and close(values(a)["tool_x"], 0.130)
    b = Bridge(a, Envelope(cfg()), org_id="org_demo", max_capability=Capability.OBSERVE, clock=Clock())
    assert b.observe().dropped == 0


def test_the_milling_profile_is_a_valid_description_with_loose_declared_ranges():
    spec = MILLING_MACHINE_SIGNALS
    assert spec.time_column == "time_machine" and spec.time_scale == 1.0 and len(spec.columns) == 6
    assert {c.unit for c in spec.columns} <= set(UNITS)
    assert all(c.minimum < 0.0 < c.maximum for c in spec.columns)


def test_a_file_is_read_only_when_it_is_a_regular_file_within_the_cap():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "data.csv"
        path.write_bytes(GOOD.encode())
        assert read_csv_file(path) == GOOD.encode() and read_csv_file(str(path), max_bytes=len(GOOD)) == GOOD.encode()
        for bad in (dict(max_bytes=len(GOOD) - 1), dict(max_bytes=0), dict(max_bytes=BYTES + 1), dict(max_bytes=True)):
            with pytest.raises(CsvError):
                read_csv_file(path, **bad)
        for bad in (d, str(Path(d) / "missing.csv"), "", 5, None, b"x", "a\x00b"):
            with pytest.raises(CsvError):
                read_csv_file(bad)


def test_a_symbolic_link_is_not_followed():
    if not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("this system has no O_NOFOLLOW")
    with tempfile.TemporaryDirectory() as d:
        target, link = Path(d) / "data.csv", Path(d) / "link.csv"
        target.write_bytes(GOOD.encode())
        try:
            link.symlink_to(target)
        except OSError:
            pytest.skip("symbolic links cannot be created here")
        with pytest.raises(CsvError):
            read_csv_file(link)


def test_a_pipe_is_refused_without_waiting_for_a_writer():
    if not hasattr(os, "mkfifo"):
        pytest.skip("this system has no named pipes")
    with tempfile.TemporaryDirectory() as d:
        fifo = os.path.join(d, "pipe.csv")
        os.mkfifo(fifo)
        outcome = []

        def attempt():
            try:
                read_csv_file(fifo)
            except CsvError:
                outcome.append("refused")

        worker = threading.Thread(target=attempt, daemon=True)
        worker.start()
        worker.join(5.0)
        waiting = worker.is_alive()
        if waiting:  # unblock a reader that waits for a writer, so the test can finish
            fd = os.open(fifo, os.O_RDWR)
            worker.join(5.0)
            os.close(fd)
        assert not waiting and outcome == ["refused"]


# ---- nothing else is in the module
def test_the_module_imports_only_the_standard_library_it_needs_and_has_no_network_code():
    tree = ast.parse(Path(csv_replay.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            imported.add((node.module or "").split(".")[0])
    assert imported == {"__future__", "dataclasses", "hashlib", "json", "math", "os", "re", "stat", "typing"}, imported


# ---- the guide stays true
def test_the_guide_example_runs_and_the_guide_says_what_it_must():
    text = DOC.read_text(encoding="utf-8")
    code = re.findall(r"```python\n(.*?)```", text, re.S)
    assert len(code) == 1
    printed = []
    scope = {"print": lambda *a: printed.append(a)}
    exec(compile(code[0], str(DOC), "exec"), scope)
    assert printed == [({"temp": 20.6, "speed": 110.0, "data_age_s": 0.5},)]
    squashed = re.sub(r"\s+", " ", text)
    for phrase in ("observe-only: it has no command and no write path", "[unverified]", "CC BY 4.0", "10.17632/zpxs87bjt8",
                   "10.1016/j.dib.2023.109574", "No file of the dataset is part of this repository"):
        assert phrase in squashed, phrase
    low = text.lower()
    for phrase in ("production-ready", "production ready", "safe for real robots", "covers all industries"):
        assert phrase not in low, phrase
