import json

import pytest

import ragleap_flows as rf
from conftest import make


def tool(i, nxt=None, **kw):
    s = {"id": i, "type": "tool", "tool": "echo", **kw}
    if nxt:
        s["next"] = nxt
    return s


def test_a_valid_flow_loads_and_has_a_stable_digest():
    f1 = make([tool("a", "b"), tool("b")])
    f2 = rf.from_json(json.dumps({"steps": [tool("a", "b"), tool("b")], "version": 1}))
    assert f1.start == "a" and f1.has_tool_steps and f1.digest == f2.digest and len(f1.digest) == 64


@pytest.mark.parametrize("bad", [
    None, [], "x", 5, {}, {"version": 1}, {"version": 2, "steps": [tool("a")]}, {"version": True, "steps": [tool("a")]},
    {"version": 1, "steps": []}, {"version": 1, "steps": "a"}, {"version": 1, "steps": [tool("a")], "extra": 1},
    {"version": 1, "steps": [tool("a")], "name": "bad name"}, {"version": 1, "steps": [tool("a")], "start": "zz"},
    {"version": 1, "steps": [tool("a")], "start": 5},
])
def test_bad_top_level_is_refused(bad):
    with pytest.raises(rf.FlowError):
        rf.from_dict(bad)
    rf.from_dict({"version": 1, "steps": [tool("a")]})  # positive control


@pytest.mark.parametrize("step", [
    {"id": "a", "type": "nope"}, {"id": "a"}, {"type": "tool", "tool": "echo"}, {"id": "a b", "type": "tool", "tool": "echo"},
    {"id": "input", "type": "tool", "tool": "echo"}, {"id": "a" * 65, "type": "tool", "tool": "echo"},
    {"id": "a", "type": "tool"}, {"id": "a", "type": "tool", "tool": "bad tool"}, {"id": "a", "type": "tool", "tool": 5},
    {"id": "a", "type": "tool", "tool": "echo", "arguments": []}, {"id": "a", "type": "tool", "tool": "echo", "extra": 1},
    {"id": "a", "type": "tool", "tool": "echo", "outbound": "yes"}, {"id": "a", "type": "tool", "tool": "echo", "external": 1},
    {"id": "a", "type": "tool", "tool": "echo", "next": "ghost"}, {"id": "a", "type": "tool", "tool": "echo", "arguments": {"x": "$bad path"}},
    {"id": "a", "type": "tool", "tool": "echo", "arguments": {"x": "$input.a.b.c.d.e.f.g.h.i"}},
    {"id": "a", "type": "tool", "tool": "echo", "arguments": {"x": float("nan")}},
    {"id": "a", "type": "approval"}, {"id": "a", "type": "approval", "prompt": ""}, {"id": "a", "type": "approval", "prompt": "x" * 501},
    {"id": "a", "type": "approval", "prompt": "ok", "show": []},
    {"id": "a", "type": "branch"}, {"id": "a", "type": "branch", "cases": []},
    {"id": "a", "type": "branch", "cases": [{"when": {"path": "$input.x", "op": "eq", "value": 1}}]},
    {"id": "a", "type": "branch", "cases": [{"when": {"path": "$input.x", "op": "zz", "value": 1}, "goto": None}]},
    {"id": "a", "type": "branch", "cases": [{"when": {"path": "$input.x", "op": "eq"}, "goto": None}]},
    {"id": "a", "type": "branch", "cases": [{"when": {"path": "$input.x", "op": "exists", "value": 1}, "goto": None}]},
    {"id": "a", "type": "branch", "cases": [{"when": {"path": "$input.x", "op": "in", "value": 3}, "goto": None}]},
    {"id": "a", "type": "branch", "cases": [{"when": {"all": []}, "goto": None}]},
    {"id": "a", "type": "branch", "cases": [{"when": {"all": [{"path": "$input.x", "op": "exists"}], "any": []}, "goto": None}]},
    {"id": "a", "type": "branch", "cases": [{"when": {"path": "$a.x", "op": "exists"}, "goto": None}]},
])
def test_bad_steps_are_refused(step):
    with pytest.raises(rf.FlowError):
        rf.from_dict({"version": 1, "steps": [step]})


def test_duplicate_ids_cycles_and_unreachable_steps_are_refused():
    with pytest.raises(rf.FlowError):
        make([tool("a"), tool("a")])
    with pytest.raises(rf.FlowError, match="cycle"):
        make([tool("a", "b"), tool("b", "a")])
    with pytest.raises(rf.FlowError, match="cycle"):
        make([tool("a", "a")])
    with pytest.raises(rf.FlowError, match="reachable"):
        make([tool("a"), tool("island")])
    make([tool("a", "b"), tool("b")])  # positive control


def test_a_path_may_only_read_a_step_that_always_runs_before_it():
    branch = {"id": "br", "type": "branch", "cases": [{"when": {"path": "$input.x", "op": "truthy"}, "goto": "left"}], "else": "right"}
    left = tool("left", "join", arguments={"value": 1})
    right = tool("right", "join")
    for reader in ("$left.value", "$right.value", "$later.value", "$join.value"):
        with pytest.raises(rf.FlowError):
            make([branch, left, right, tool("join", arguments={"value": reader})])
    make([branch, left, right, tool("join", arguments={"value": "$input.x"})])
    make([tool("a", "b"), tool("b", arguments={"value": "$a.value"})])  # positive control


def test_the_output_may_name_only_real_steps():
    with pytest.raises(rf.FlowError):
        make([tool("a")], output={"x": "$ghost.y"})
    make([tool("a")], output={"x": "$a.y", "lit": "$$not-a-path"})


def test_size_and_step_limits():
    chain = lambda n: [tool("s%d" % i, "s%d" % (i + 1) if i < n - 1 else None) for i in range(n)]
    assert len(make(chain(100)).steps) == 100
    with pytest.raises(rf.FlowError, match="1 to 100"):
        make(chain(101))                                           # one connected chain: only the step cap can refuse it
    big = {"version": 1, "steps": [tool("a", arguments={"v": "x" * 300_000})]}
    with pytest.raises(rf.FlowError, match="too large"):
        rf.from_dict(big)
    with pytest.raises(rf.FlowError, match="too large"):
        rf.from_json(json.dumps(big))
    with pytest.raises(rf.FlowError, match="too large"):
        rf.from_json(json.dumps(big).encode())


@pytest.mark.parametrize("text", ["", "{", "[1]", '{"version":1,"steps":[{"id":"a","type":"tool","tool":"echo"}],"version":1}', '{"version":NaN,"steps":[]}',
                                  '{"version":1,"steps":[{"id":"a","type":"tool","tool":"echo","arguments":{"v":Infinity}}]}'])
def test_bad_json_text_is_refused(text):
    with pytest.raises(rf.FlowError):
        rf.from_json(text)


def test_non_text_and_bad_bytes_are_refused():
    for bad in (5, None, b"\xff\xfe"):
        with pytest.raises(rf.FlowError):
            rf.from_json(bad)


def test_deeply_nested_json_does_not_crash():
    with pytest.raises(rf.FlowError):
        rf.from_json("[" * 100000)
    with pytest.raises(rf.FlowError):
        rf.from_dict({"version": 1, "steps": [tool("a", arguments={"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": {"i": {"j": 1}}}}}}}}}})]})


def test_a_huge_text_with_a_small_flow_inside_is_refused_by_the_text_limit():
    small = json.dumps({"version": 1, "steps": [tool("a")]})
    assert rf.from_json(small + " " * 100).start == "a"           # positive control
    with pytest.raises(rf.FlowError, match="too large"):
        rf.from_json(small + " " * 300_000)
    with pytest.raises(rf.FlowError, match="too large"):
        rf.from_json((small + " " * 300_000).encode())


def test_a_double_dollar_is_a_literal_in_values_and_refused_as_a_path():
    from ragleap_flows.flow import parse_path

    with pytest.raises(rf.FlowError):
        parse_path("$$input.x")
    assert parse_path("$input.x") == ("input", ["x"])
    with pytest.raises(rf.FlowError):
        make([{"id": "br", "type": "branch", "cases": [{"when": {"path": "$$input.x", "op": "exists"}, "goto": None}]}])
    make([tool("a", arguments={"v": "$$input.x"})])               # a literal is fine in a value
