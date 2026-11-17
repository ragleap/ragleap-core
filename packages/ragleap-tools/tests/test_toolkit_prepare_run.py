"""Toolkit.prepare and Toolkit.run: the two halves of Toolkit.call."""
import logging

import pytest

from ragleap_tools import Tool, ToolError, ToolResult, Toolkit


def echo_tool(name="echo", props=None, required=None, record=None):
    def handler(**kw):
        if record is not None:
            record.append(kw)
        return ToolResult(success=True, result=kw)

    return Tool(
        name=name,
        description="d",
        parameters={"type": "object", "properties": props if props is not None else {"x": {"type": "integer"}},
                    "required": required if required is not None else []},
        handler=handler,
    )


def raising_tool(exc, name="boom"):
    def handler(**kw):
        raise exc

    return Tool(name=name, description="d", parameters={"type": "object", "properties": {}}, handler=handler)


def test_prepare_returns_the_registered_tool_and_a_new_arguments_dict():
    t = echo_tool()
    kit = Toolkit([t])
    sent = {"x": 1}
    out = kit.prepare("echo", sent)
    assert isinstance(out, tuple) and len(out) == 2
    tool, args = out
    assert tool is t and args == {"x": 1}
    assert args is not sent
    args["x"] = 99
    assert sent == {"x": 1}


@pytest.mark.parametrize(
    "arguments, expected",
    [('{"x": 1}', {"x": 1}), ({"x": 2}, {"x": 2}), (None, {}), ("", {}), ("  \n", {})],
)
def test_prepare_accepts_a_json_string_a_dict_or_nothing(arguments, expected):
    out = Toolkit([echo_tool()]).prepare("echo", arguments)
    assert out[1] == expected


def test_prepare_never_runs_the_handler():
    rec = []
    kit = Toolkit([echo_tool(record=rec)])
    kit.prepare("echo", {"x": 1})
    kit.prepare("echo", '{"x": "no"}')
    kit.prepare("nope", {})
    assert rec == []


def test_prepare_drops_null_optional_arguments_like_call_does():
    out = Toolkit([echo_tool()]).prepare("echo", '{"x": null}')
    assert out[1] == {}


@pytest.mark.parametrize(
    "name, arguments, error",
    [
        ("nope", {}, "unknown tool 'nope'"),
        (None, {}, "unknown tool 'None'"),
        ("echo", "{not json", "arguments are not valid JSON"),
        ("echo", "[1, 2]", "arguments must be a JSON object"),
        ("echo", 5, "arguments must be a JSON object"),
        ("echo", {1: "x"}, "argument names must be strings"),
        ("echo", '{"x": "no"}', "invalid arguments: argument 'x' must be of type integer"),
        ("echo", '{"zzz": 1}', "invalid arguments: unknown argument 'zzz'"),
    ],
)
def test_prepare_returns_the_failed_result_call_would_return(name, arguments, error):
    kit = Toolkit([echo_tool()])
    prepared = kit.prepare(name, arguments)
    assert isinstance(prepared, ToolResult)
    assert prepared == ToolResult(success=False, error=error)
    assert kit.call(name, arguments) == prepared


def test_prepare_reports_a_missing_required_argument():
    kit = Toolkit([echo_tool(required=["x"])])
    expected = ToolResult(success=False, error="invalid arguments: missing required argument 'x'")
    assert kit.prepare("echo", {}) == expected


def test_prepare_enforces_the_size_limit_before_parsing():
    kit = Toolkit([echo_tool()], max_arguments_chars=20)
    assert kit.prepare("echo", '{"x": ' + "1" * 50 + "}").error == "arguments are too large (max 20 characters)"


def test_run_runs_the_handler_with_keyword_arguments():
    rec = []
    kit = Toolkit([echo_tool(record=rec)])
    tool, args = kit.prepare("echo", {"x": 3})
    assert kit.run(tool, args) == ToolResult(success=True, result={"x": 3})
    assert rec == [{"x": 3}]


def test_run_goes_through_tool_call_like_the_old_call_did():
    seen = []

    class Spy(Tool):
        def call(self, /, **kwargs):
            seen.append(kwargs)
            return ToolResult(success=True, result="via call")

    t = Spy(name="spy", description="d", parameters={"type": "object", "properties": {}},
            handler=lambda **kw: ToolResult(success=True, result="via handler"))
    kit = Toolkit([t])
    assert kit.run(t, {}) == ToolResult(success=True, result="via call")
    assert kit.call("spy", {}) == ToolResult(success=True, result="via call")
    assert seen == [{}, {}]


def test_run_passes_an_argument_literally_named_self():
    rec = []
    kit = Toolkit([echo_tool(props={"self": {"type": "string"}}, record=rec)])
    tool, args = kit.prepare("echo", {"self": "x"})
    assert kit.run(tool, args).success is True
    assert rec == [{"self": "x"}]


def test_run_turns_tool_error_into_a_failed_result_with_the_message():
    kit = Toolkit([raising_tool(ToolError("not allowed"))])
    tool, args = kit.prepare("boom", {})
    assert kit.run(tool, args) == ToolResult(success=False, error="not allowed")


def test_run_contains_unexpected_exceptions_and_keeps_the_detail_out_of_the_result(caplog):
    kit = Toolkit([raising_tool(RuntimeError("secret connection string"))])
    tool, args = kit.prepare("boom", {})
    with caplog.at_level(logging.WARNING, logger="ragleap_tools"):
        r = kit.run(tool, args)
    assert r == ToolResult(success=False, error="boom failed (RuntimeError)")
    assert any(rec.exc_info for rec in caplog.records)


def test_run_fails_cleanly_when_the_handler_returns_something_else():
    t = Tool(name="odd", description="d", parameters={"type": "object", "properties": {}}, handler=lambda **kw: "text")
    kit = Toolkit([t])
    assert kit.run(t, {}) == ToolResult(success=False, error="odd returned an invalid result")


def test_run_refuses_a_tool_that_is_not_in_this_toolkit_and_does_not_run_it():
    rec = []
    kit = Toolkit([echo_tool(record=rec)])
    stranger = echo_tool("echo", record=rec)
    assert kit.run(stranger, {}) == ToolResult(success=False, error="unknown tool 'echo'")
    assert rec == []


@pytest.mark.parametrize("not_a_tool", [None, "echo", 5, {"name": "echo"}, object()])
def test_run_refuses_values_that_are_not_tools(not_a_tool):
    r = Toolkit([echo_tool()]).run(not_a_tool, {})
    assert r.success is False and r.error.startswith("unknown tool")


@pytest.mark.parametrize("arguments", [None, "{}", [1], 5, ("x", 1)])
def test_run_refuses_arguments_that_are_not_a_dict(arguments):
    rec = []
    t = echo_tool(record=rec)
    assert Toolkit([t]).run(t, arguments) == ToolResult(success=False, error="arguments must be a JSON object")
    assert rec == []


def test_run_refuses_non_string_argument_names():
    rec = []
    t = echo_tool(record=rec)
    assert Toolkit([t]).run(t, {1: "x"}) == ToolResult(success=False, error="argument names must be strings")
    assert rec == []


def test_a_tool_shared_with_a_selected_toolkit_can_be_run_by_either():
    t = echo_tool()
    kit = Toolkit([t, echo_tool("other")])
    sub = kit.select(include=["echo"])
    assert kit.run(t, {}).success is True and sub.run(t, {}).success is True


def test_a_tool_renamed_after_it_was_added_still_works_under_its_registered_name():
    t = echo_tool()
    kit = Toolkit([t])
    t.name = "renamed"
    assert kit.call("echo", {}).success is True
    assert kit.run(t, {}).success is True


@pytest.mark.parametrize(
    "name, arguments",
    [
        ("echo", {"x": 1}),
        ("echo", '{"x": 2}'),
        ("echo", None),
        ("echo", '{"x": null}'),
        ("nope", {}),
        ("echo", "{bad"),
        ("echo", '{"x": "s"}'),
    ],
)
def test_call_is_prepare_followed_by_run(name, arguments):
    kit = Toolkit([echo_tool()])
    prepared = kit.prepare(name, arguments)
    expected = prepared if isinstance(prepared, ToolResult) else kit.run(*prepared)
    assert kit.call(name, arguments) == expected
