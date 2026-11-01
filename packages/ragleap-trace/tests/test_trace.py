import asyncio
import json
import os
import stat

import pytest

import ragleap_trace as rt
from ragleap_trace import InMemoryExporter, JsonlExporter, Tracer, traced


def make():
    exp = InMemoryExporter()
    return Tracer([exp]), exp


# ---- basics -----------------------------------------------------------------


def test_zero_dependencies_and_version():
    assert rt.__version__ == "0.1.0"


def test_span_recorded_with_timing_and_attributes():
    tracer, exp = make()
    with tracer.span("work", user_count=3, ok=True, ratio=0.5, tag="x") as s:
        s.set_attribute("later", 1)
    assert len(exp.spans) == 1
    d = exp.spans[0]
    assert d["name"] == "work"
    assert d["status"] == "ok"
    assert d["duration_ms"] is not None and d["duration_ms"] >= 0
    assert d["attributes"] == {"user_count": 3, "ok": True, "ratio": 0.5, "tag": "x", "later": 1}


def test_parent_child_share_trace_id():
    tracer, exp = make()
    with tracer.span("outer"):
        with tracer.span("inner"):
            pass
    inner, outer = exp.spans
    assert inner["parent_id"] == outer["span_id"]
    assert inner["trace_id"] == outer["trace_id"]
    assert outer["parent_id"] is None


def test_siblings_do_not_nest():
    tracer, exp = make()
    with tracer.span("a"):
        pass
    with tracer.span("b"):
        pass
    a, b = exp.spans
    assert b["parent_id"] is None
    assert a["trace_id"] != b["trace_id"]


# ---- span recorded on exception --------------------------------------------


def test_span_recorded_on_exception_and_exception_propagates():
    tracer, exp = make()
    with pytest.raises(ValueError):
        with tracer.span("boom"):
            raise ValueError("secret user text")
    d = exp.spans[0]
    assert d["status"] == "error"
    assert d["error_type"] == "ValueError"


def test_exception_message_never_recorded():
    tracer, exp = make()
    with pytest.raises(ValueError):
        with tracer.span("boom"):
            raise ValueError("hunter2-private")
    assert "hunter2-private" not in json.dumps(exp.spans)


# ---- limits -----------------------------------------------------------------


def test_string_values_truncated():
    tracer, exp = make()
    with tracer.span("t", note="a" * (rt.MAX_STR_LEN + 500)):
        pass
    assert len(exp.spans[0]["attributes"]["note"]) == rt.MAX_STR_LEN


def test_name_truncated():
    tracer, exp = make()
    with tracer.span("n" * (rt.MAX_NAME_LEN + 50)):
        pass
    assert len(exp.spans[0]["name"]) == rt.MAX_NAME_LEN


def test_key_truncated():
    tracer, exp = make()
    key = "k" * (rt.MAX_KEY_LEN + 20)
    with tracer.span("t", **{key: 1}):
        pass
    assert list(exp.spans[0]["attributes"]) == ["k" * rt.MAX_KEY_LEN]


def test_attribute_count_limited_and_dropped_counted():
    tracer, exp = make()
    with tracer.span("t") as s:
        for i in range(rt.MAX_ATTRIBUTES + 10):
            s.set_attribute(f"k{i}", i)
    d = exp.spans[0]
    assert len(d["attributes"]) == rt.MAX_ATTRIBUTES
    assert d["dropped_attributes"] == 10


def test_existing_key_can_be_updated_at_the_limit():
    tracer, exp = make()
    with tracer.span("t") as s:
        for i in range(rt.MAX_ATTRIBUTES):
            s.set_attribute(f"k{i}", i)
        assert s.set_attribute("k0", 99) is True
    assert exp.spans[0]["attributes"]["k0"] == 99


# ---- value types ------------------------------------------------------------


class Secret:
    def __repr__(self):
        return "LEAKED-REPR"


def test_only_simple_types_kept_objects_never_repr():
    tracer, exp = make()
    with tracer.span("t") as s:
        assert s.set_attribute("obj", Secret()) is False
        assert s.set_attribute("lst", [1, 2]) is False
        assert s.set_attribute("dct", {"a": 1}) is False
        assert s.set_attribute("none", None) is False
        assert s.set_attribute("bytes", b"x") is False
    d = exp.spans[0]
    assert d["attributes"] == {}
    assert d["dropped_attributes"] == 5
    assert "LEAKED-REPR" not in json.dumps(d)


def test_non_finite_floats_dropped():
    tracer, exp = make()
    with tracer.span("t") as s:
        assert s.set_attribute("a", float("nan")) is False
        assert s.set_attribute("b", float("inf")) is False
    assert exp.spans[0]["attributes"] == {}


def test_non_string_or_empty_keys_dropped():
    tracer, exp = make()
    with tracer.span("t") as s:
        assert s.set_attribute(5, 1) is False
        assert s.set_attribute("", 1) is False
    assert exp.spans[0]["dropped_attributes"] == 2


# ---- content off by default -------------------------------------------------


@pytest.mark.parametrize(
    "key",
    ["prompt", "answer", "file_name", "filename", "Prompt", "FILE-NAME", "api-key", " query "],
)
def test_content_keys_never_recorded(key):
    tracer, exp = make()
    with tracer.span("t") as s:
        assert s.set_attribute(key, "private text") is False
    d = exp.spans[0]
    assert d["attributes"] == {}
    assert d["dropped_attributes"] == 1
    assert "private text" not in json.dumps(d)


def test_content_keys_blocked_when_passed_as_span_kwargs():
    tracer, exp = make()
    with tracer.span("t", prompt="private text", docs=3):
        pass
    d = exp.spans[0]
    assert d["attributes"] == {"docs": 3}
    assert "private text" not in json.dumps(d)


# ---- exporter failures ------------------------------------------------------


class Broken:
    def export(self, span):
        raise RuntimeError("disk full")


def test_exporter_error_never_reaches_caller_and_is_counted():
    exp = InMemoryExporter()
    tracer = Tracer([Broken(), exp])
    with tracer.span("t"):
        result = 41 + 1
    assert result == 42
    assert tracer.dropped == 1
    assert len(exp.spans) == 1  # healthy exporter still receives the span


def test_exporter_error_does_not_mask_caller_exception():
    tracer = Tracer([Broken()])
    with pytest.raises(KeyError):
        with tracer.span("t"):
            raise KeyError("x")
    assert tracer.dropped == 1


# ---- decorator and async ----------------------------------------------------


def test_traced_sync_and_name():
    tracer, exp = make()

    @traced(tracer)
    def add(a, b):
        return a + b

    assert add(1, 2) == 3
    assert add.__name__ == "add"
    assert exp.spans[0]["name"].endswith("add")


def test_traced_custom_name():
    tracer, exp = make()

    @traced(tracer, name="custom")
    def f():
        return 1

    f()
    assert exp.spans[0]["name"] == "custom"


def test_traced_async_and_async_with_nesting():
    tracer, exp = make()

    @traced(tracer, name="child")
    async def child():
        await asyncio.sleep(0)
        return 7

    async def main():
        async with tracer.span("parent"):
            return await child()

    assert asyncio.run(main()) == 7
    child_d, parent_d = exp.spans
    assert child_d["parent_id"] == parent_d["span_id"]


def test_concurrent_async_tasks_keep_separate_parents():
    tracer, exp = make()

    async def task(name):
        async with tracer.span(name):
            await asyncio.sleep(0)
            async with tracer.span(name + "-child"):
                await asyncio.sleep(0)

    async def main():
        await asyncio.gather(task("a"), task("b"))

    asyncio.run(main())
    by_name = {s["name"]: s for s in exp.spans}
    assert by_name["a-child"]["parent_id"] == by_name["a"]["span_id"]
    assert by_name["b-child"]["parent_id"] == by_name["b"]["span_id"]
    assert by_name["a"]["trace_id"] != by_name["b"]["trace_id"]


# ---- JSONL exporter ---------------------------------------------------------


def test_jsonl_writes_lines_with_mode_0600(tmp_path):
    p = tmp_path / "t.jsonl"
    exp = JsonlExporter(p)
    tracer = Tracer([exp])
    with tracer.span("a", n=1):
        pass
    with tracer.span("b"):
        pass
    exp.close()
    lines = p.read_text().splitlines()
    assert [json.loads(x)["name"] for x in lines] == ["a", "b"]
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="posix permissions")
def test_jsonl_refuses_existing_world_readable_file(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text("")
    os.chmod(p, 0o644)
    with pytest.raises(PermissionError):
        JsonlExporter(p)


def test_jsonl_export_after_close_is_counted_not_raised(tmp_path):
    exp = JsonlExporter(tmp_path / "t.jsonl")
    tracer = Tracer([exp])
    exp.close()
    with tracer.span("t"):
        pass
    assert tracer.dropped == 1


def test_jsonl_appends_across_instances(tmp_path):
    p = tmp_path / "t.jsonl"
    for name in ("a", "b"):
        e = JsonlExporter(p)
        with Tracer([e]).span(name):
            pass
        e.close()
    assert len(p.read_text().splitlines()) == 2


def test_async_exception_recorded_and_propagates():
    tracer, exp = make()

    async def main():
        async with tracer.span("aboom"):
            raise KeyError("x")

    with pytest.raises(KeyError):
        asyncio.run(main())
    d = exp.spans[0]
    assert d["status"] == "error" and d["error_type"] == "KeyError"
