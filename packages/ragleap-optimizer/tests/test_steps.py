import copy
import json

import pytest

from ragleap_optimizer import InvalidRequestError, estimate_tokens, steps

C = estimate_tokens
BIG = "x" * 300


def run(step, request):
    return step(request, C)


def req(*msgs):
    return {"messages": [{"role": r, "content": c} for r, c in msgs]}


# ------------------------------------------------------------------ dedupe_context

def test_dedupe_removes_earlier_copies_and_keeps_the_last():
    r = req(("user", "question"), ("tool", BIG), ("assistant", "ok"), ("tool", BIG), ("user", "more"), ("tool", BIG))
    out, lossy = run(steps.dedupe_context(), r)
    assert [m["role"] for m in out["messages"]] == ["user", "assistant", "user", "tool"]
    assert out["messages"][-1]["content"] == BIG and lossy is False


def test_dedupe_never_touches_system_or_short_messages():
    r = req(("system", BIG), ("system", BIG), ("user", "yes"), ("user", "yes"), ("user", "a" * 199), ("user", "a" * 199))
    out, _ = run(steps.dedupe_context(), r)
    assert out == r


def test_dedupe_keeps_same_text_with_a_different_role():
    r = req(("tool", BIG), ("user", BIG))
    assert run(steps.dedupe_context(), r)[0] == r


def test_dedupe_handles_list_content_and_min_chars():
    parts = [{"type": "text", "text": BIG}]
    r = {"messages": [{"role": "tool", "content": parts}, {"role": "user", "content": "x"}, {"role": "tool", "content": copy.deepcopy(parts)}]}
    assert len(run(steps.dedupe_context(), r)[0]["messages"]) == 2
    short = req(("tool", "y" * 50), ("tool", "y" * 50))
    assert len(run(steps.dedupe_context(min_chars=40), short)[0]["messages"]) == 1
    assert len(run(steps.dedupe_context(min_chars=60), short)[0]["messages"]) == 2


def test_dedupe_does_not_change_its_input():
    r = req(("tool", BIG), ("tool", BIG))
    before = copy.deepcopy(r)
    run(steps.dedupe_context(), r)
    assert r == before


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "5", None])
def test_dedupe_rejects_bad_min_chars(bad):
    with pytest.raises(InvalidRequestError):
        steps.dedupe_context(min_chars=bad)


# ------------------------------------------------------------------ trim_tool_output

LONG = "HEAD-" + "m" * 3000 + "-TAIL"


def test_trim_keeps_start_and_end_marks_the_cut_and_is_lossy():
    out, lossy = run(steps.trim_tool_output(100), req(("tool", LONG)))
    text = out["messages"][0]["content"]
    assert lossy is True
    assert text.startswith("HEAD-") and text.endswith("-TAIL")
    assert "omitted by ragleap-optimizer" in text
    assert C(text) <= 100 + 30 < C(LONG)


def test_trim_marker_counts_the_omitted_characters():
    out, _ = run(steps.trim_tool_output(100, keep_tail_fraction=0.5), req(("tool", LONG)))
    text = out["messages"][0]["content"]
    head, rest = text.split("\n[... ", 1)
    omitted = int(rest.split(" ", 1)[0])
    tail = rest.split("...]\n", 1)[1]
    assert len(head) + omitted + len(tail) == len(LONG)


def test_trim_leaves_short_content_and_other_roles_alone():
    r = req(("tool", "short"), ("user", LONG), ("assistant", LONG))
    out, lossy = run(steps.trim_tool_output(100), r)
    assert out == r and lossy is False
    out, lossy = run(steps.trim_tool_output(100, roles=("tool", "user")), r)
    assert out["messages"][1]["content"] != LONG and out["messages"][2]["content"] == LONG and lossy is True


def test_trim_does_not_touch_non_string_content():
    r = {"messages": [{"role": "tool", "content": [{"type": "text", "text": LONG}]}]}
    assert run(steps.trim_tool_output(100), r) == (r, False)


def test_trim_skips_when_the_marker_would_cost_more_than_it_saves():
    text = "z" * 31                                  # 11 tokens, cap 10: cutting would add a longer marker
    r = req(("tool", text))
    out, lossy = run(steps.trim_tool_output(10), r)
    assert out == r and lossy is False


def test_trim_without_tail():
    out, _ = run(steps.trim_tool_output(100, keep_tail_fraction=0), req(("tool", LONG)))
    assert out["messages"][0]["content"].endswith("...]\n")


@pytest.mark.parametrize("bad", [0, -1, True, 2.5, "9"])
def test_trim_rejects_bad_max_tokens(bad):
    with pytest.raises(InvalidRequestError):
        steps.trim_tool_output(bad)


@pytest.mark.parametrize("bad", [-0.1, 1, 1.5, True, "0.5", None])
def test_trim_rejects_bad_tail_fraction(bad):
    with pytest.raises(InvalidRequestError):
        steps.trim_tool_output(100, keep_tail_fraction=bad)


# ------------------------------------------------------------------ minify_whitespace

def test_minify_collapses_blank_lines_only_from_three_newlines():
    out, lossy = run(steps.minify_whitespace(), req(("user", "a\n\n\n\n\nb\n\nc\nd")))
    assert out["messages"][0]["content"] == "a\n\nb\n\nc\nd" and lossy is False


def test_minify_writes_json_compactly_without_changing_the_data():
    data = {"name": "caf\u00e9", "items": [1, 2, {"k": None}], "ok": True}
    out, _ = run(steps.minify_whitespace(), req(("tool", json.dumps(data, indent=4, ensure_ascii=False))))
    text = out["messages"][0]["content"]
    assert json.loads(text) == data and " " not in text and "caf\u00e9" in text


def test_minify_leaves_non_json_that_looks_like_json():
    out, _ = run(steps.minify_whitespace(), req(("user", "{not json at all}\n\n\n\nend")))
    assert out["messages"][0]["content"] == "{not json at all}\n\nend"


def test_minify_ignores_list_content():
    r = {"messages": [{"role": "user", "content": [{"type": "text", "text": "a\n\n\n\nb"}]}]}
    assert run(steps.minify_whitespace(), r)[0] == r


# ------------------------------------------------------------------ stable_prefix

def test_stable_prefix_moves_system_messages_first_and_keeps_other_order():
    r = req(("user", "1"), ("system", "S1"), ("assistant", "2"), ("system", "S2"), ("user", "3"))
    out, lossy = run(steps.stable_prefix(), r)
    assert [m["content"] for m in out["messages"]] == ["S1", "S2", "1", "2", "3"] and lossy is False


def test_stable_prefix_makes_tool_json_independent_of_key_order():
    a = {"messages": [], "tools": [{"type": "function", "function": {"name": "f", "description": "d", "parameters": {"a": 1, "b": 2}}}]}
    b = {"messages": [], "tools": [{"function": {"parameters": {"b": 2, "a": 1}, "description": "d", "name": "f"}, "type": "function"}]}
    ja = json.dumps(run(steps.stable_prefix(), a)[0]["tools"])
    jb = json.dumps(run(steps.stable_prefix(), b)[0]["tools"])
    assert ja == jb


def test_stable_prefix_tool_order_only_changes_when_asked():
    tools = [{"type": "function", "function": {"name": n}} for n in ("zeta", "alpha", "mid")]
    r = {"messages": [], "tools": tools}
    keep = run(steps.stable_prefix(), r)[0]["tools"]
    assert [t["function"]["name"] for t in keep] == ["zeta", "alpha", "mid"]
    srt = run(steps.stable_prefix(sort_tools=True), r)[0]["tools"]
    assert [t["function"]["name"] for t in srt] == ["alpha", "mid", "zeta"]


def test_stable_prefix_without_tools_is_fine():
    assert "tools" not in run(steps.stable_prefix(), req(("user", "x")))[0]


def test_trim_acts_above_the_cap_and_not_at_it():
    at_cap = "k" * 3000                                 # exactly 1000 tokens: a cut here would really save tokens
    over = "k" * 3030                                   # 1010 tokens
    r1, l1 = run(steps.trim_tool_output(1000), req(("tool", at_cap)))
    assert r1["messages"][0]["content"] == at_cap and l1 is False
    r2, l2 = run(steps.trim_tool_output(1000), req(("tool", over)))
    assert r2["messages"][0]["content"] != over and l2 is True
