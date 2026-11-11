"""UTCP 0.5.0: the one accepted auth shape and owner-allowed static query parameters.

Recording fake transport only; placeholder credentials only. Helpers are repeated from
test_utcp.py on purpose so this file stands alone.
"""
import json

import pytest

from ragleap_integrations import UtcpConfigError, UtcpServerConfig, connect_utcp
from ragleap_integrations._net import HttpResponse

MANUAL_URL = "https://api.example.com/utcp"
TOKEN = "example-token-0001"
BEARER = "Bearer ${EXAMPLE_API_KEY}"
AUTH = {"auth_type": "api_key", "api_key": BEARER, "var_name": "Authorization", "location": "header"}
REAL_INPUTS = {"type": "object", "properties": {"arguments": {"type": "object"}}, "required": ["arguments"]}
CALL_URL = "https://api.example.com/tools/list_things/call?format=utcp"


def tool(name="t", method="POST", url=CALL_URL, inputs=None, **template):
    t = {"call_template_type": "http", "http_method": method, "url": url}
    t.update(template)
    return {"name": name, "description": "tool " + name,
            "inputs": inputs if inputs is not None else {"type": "object", "properties": {"id": {"type": "string"}}},
            "tool_call_template": t}


def real_tool(name="list_things", **over):
    """The shape observed on every tool of a real public manual (2026-11-08/09)."""
    t = {"call_template_type": "http", "http_method": "POST", "name": "x", "auth": dict(AUTH),
         "url": "https://api.example.com/tools/%s/call?format=utcp" % name,
         "body_field": "arguments", "content_type": "application/json"}
    t.update(over)
    return {"name": name, "description": "d", "inputs": REAL_INPUTS, "tool_call_template": t}


def manual(*tools):
    return {"utcp_version": "1.0.1", "tools": list(tools)}


class Fake:
    def __init__(self, doc):
        self.doc, self.calls = doc, []

    def __call__(self, method, url, headers, body=None):
        self.calls.append((method, url, dict(headers), body))
        if url == MANUAL_URL:
            return HttpResponse(200, {}, json.dumps(self.doc).encode(), "application/json")
        return HttpResponse(200, {}, b'{"ok": true}', "application/json")

    @property
    def tool_calls(self):
        return [c for c in self.calls if c[1] != MANUAL_URL]


def connect(doc, allow=("t",), **kw):
    fake = Fake(doc)
    return fake, connect_utcp(MANUAL_URL, allow=list(allow), transport=fake, **kw)


def reason(doc, name="t", **kw):
    _, conn = connect(doc, allow=[name], **kw)
    assert conn.tools == [], "the tool should not be offered"
    assert len(conn.skipped) == 1
    return conn.skipped[0][1]


OLD_AUTH = "the template declares auth (not supported; the owner token is sent as a bearer token)"
NO_TOKEN = "the template declares auth but no token is configured"
OLD_QUERY = "url has a query string or a fragment"
NOT_ALLOWED = "url has a query parameter the owner did not allow"


# ---------------------------------------------------------------- auth shape

def test_the_exact_auth_block_is_accepted_with_a_token():
    fake, conn = connect(manual(tool(auth=dict(AUTH), url="https://api.example.com/o")), token=TOKEN)
    assert [t.name for t in conn.tools] == ["utcp__t"] and conn.skipped == []


def test_the_exact_auth_block_without_a_token_is_skipped_with_a_hint():
    doc = manual(tool(auth=dict(AUTH), url="https://api.example.com/o"))
    _, conn = connect(doc)
    assert conn.skipped == [("utcp.t", NO_TOKEN)]
    assert "skipped: utcp.t - " + NO_TOKEN + " (pass token)" in conn.report()


@pytest.mark.parametrize("change", [
    {"var_name": "X-Api-Key"}, {"var_name": "authorization"}, {"location": "query"}, {"location": "cookie"},
    {"location": "Header"}, {"auth_type": "basic"}, {"auth_type": "oauth2"}, {"auth_type": "API_KEY"},
    {"api_key": "${EXAMPLE_API_KEY}"}, {"api_key": "bearer ${EXAMPLE_API_KEY}"},
    {"api_key": "Basic ${EXAMPLE_API_KEY}"}, {"api_key": "Bearer ${A} ${B}"}, {"api_key": "Bearer ${1A}"},
    {"api_key": "Bearer ${}"}, {"api_key": "Bearer ${" + "A" * 65 + "}"}, {"api_key": BEARER + " "},
    {"api_key": BEARER + "\n"}, {"api_key": "Bearer $EXAMPLE_API_KEY"}, {"api_key": "Bearer literal-value"},
    {"api_key": " " + BEARER}, {"api_key": 5}, {"api_key": None},
])
def test_near_miss_auth_blocks_keep_the_old_reason(change):
    auth = dict(AUTH); auth.update(change)
    assert reason(manual(tool(auth=auth, url="https://api.example.com/o")), token=TOKEN) == OLD_AUTH


@pytest.mark.parametrize("auth", [
    dict(AUTH, extra="x"), {k: v for k, v in AUTH.items() if k != "location"},
    {k: v for k, v in AUTH.items() if k != "api_key"}, {}, "Bearer x", ["a"], 5, True,
])
def test_wrong_shaped_auth_keeps_the_old_reason(auth):
    assert reason(manual(tool(auth=auth, url="https://api.example.com/o")), token=TOKEN) == OLD_AUTH


def test_auth_tools_is_still_refused_even_with_the_exact_auth():
    doc = manual(tool(auth=dict(AUTH), auth_tools={"x": 1}, url="https://api.example.com/o"))
    assert reason(doc, token=TOKEN) == OLD_AUTH
    assert reason(manual(tool(auth_tools={"x": 1}, url="https://api.example.com/o")), token=TOKEN) == OLD_AUTH


def test_auth_null_still_means_no_auth():
    _, conn = connect(manual(tool(auth=None, url="https://api.example.com/o")))
    assert [t.name for t in conn.tools] == ["utcp__t"]


def test_the_placeholder_name_is_never_looked_up(monkeypatch):
    monkeypatch.setenv("EXAMPLE_API_KEY", "example-env-marker-9999")
    fake, conn = connect(manual(tool(auth=dict(AUTH), url="https://api.example.com/o")), token=TOKEN)
    conn.client.call_tool("utcp", "t", {}, True)
    assert "example-env-marker-9999" not in repr(fake.calls)
    assert all(c[2]["Authorization"] == "Bearer " + TOKEN for c in fake.calls)


def test_an_accepted_auth_does_not_widen_the_approved_origins():
    doc = manual(tool(auth=dict(AUTH), url="https://other.example.net/o"))
    assert reason(doc, token=TOKEN) == "url is outside the approved origins"


# --------------------------------------------------------------- query rules

def test_by_default_a_query_string_is_refused_as_in_0_4_0():
    assert reason(manual(tool())) == OLD_QUERY


def test_an_allowed_query_name_is_accepted_and_kept_apart_from_the_url():
    fake, conn = connect(manual(tool()), allowed_query=["format"])
    assert [t.name for t in conn.tools] == ["utcp__t"]


@pytest.mark.parametrize("url,expected", [
    ("https://api.example.com/o?other=1", NOT_ALLOWED),
    ("https://api.example.com/o?format=utcp&other=1", NOT_ALLOWED),
    ("https://api.example.com/o?Format=utcp", NOT_ALLOWED),
    ("https://api.example.com/o?format=utcp#frag", OLD_QUERY),
    ("https://api.example.com/o?format=a%20b", "url has a query parameter with an unusable value"),
    ("https://api.example.com/o?format=a+b", "url has a query parameter with an unusable value"),
    ("https://api.example.com/o?format=a/b", "url has a query parameter with an unusable value"),
    ("https://api.example.com/o?format=a;b", "url has a query parameter with an unusable value"),
    ("https://api.example.com/o?format={x}", "url has a query parameter with an unusable value"),
    ("https://api.example.com/o?format=" + "a" * 65, "url has a query parameter with an unusable value"),
    ("https://api.example.com/o?format=", "url has a malformed query string"),
    ("https://api.example.com/o?format", "url has a malformed query string"),
    ("https://api.example.com/o?=utcp", "url has a malformed query string"),
    ("https://api.example.com/o?format=utcp&", "url has a malformed query string"),
    ("https://api.example.com/o?", OLD_QUERY),
    ("https://api.example.com/o?#x", OLD_QUERY),
    ("https://api.example.com/o?format=${X}", "url contains a variable placeholder (not supported)"),
    ("https://api.example.com/o?format=$X", "url contains a variable placeholder (not supported)"),
    ("https://api.example.com/o?format=utcp&format=json", "url repeats a query parameter"),
    ("https://api.example.com/o?id=utcp", "a query parameter is also an input property"),
])
def test_query_refusals(url, expected):
    doc = manual(tool(url=url))
    assert reason(doc, allowed_query=["format", "id"]) == expected


def test_too_many_query_fields_are_refused():
    names = ["p%d" % i for i in range(8)]
    ok = "https://api.example.com/o?" + "&".join(n + "=v" for n in names)
    _, conn = connect(manual(tool(url=ok)), allowed_query=names)
    assert len(conn.tools) == 1
    names9 = names + ["p8"]
    too_many = "https://api.example.com/o?" + "&".join(n + "=v" for n in names9)
    assert reason(manual(tool(url=too_many)), allowed_query=names) == "url has too many query parameters"


def test_the_query_hint_is_in_the_report():
    _, conn = connect(manual(tool(url="https://api.example.com/o?other=1")), allowed_query=["format"])
    assert "(list the name in allowed_query)" in conn.report()


@pytest.mark.parametrize("bad", [
    "format", {"format"}, ["bad name"], ["a/b"], [""], ["x" * 33], ["a", "a"], ["p%d" % i for i in range(9)],
    [5], None, 5,
])
def test_allowed_query_validation(bad):
    with pytest.raises(UtcpConfigError):
        UtcpServerConfig(name="utcp", url=MANUAL_URL, allowed_query=bad)


def test_allowed_query_validation_accepts_edges():
    UtcpServerConfig(name="utcp", url=MANUAL_URL, allowed_query=("x" * 32, "a.b-c_d"))
    UtcpServerConfig(name="utcp", url=MANUAL_URL, allowed_query=["p%d" % i for i in range(8)])
    UtcpServerConfig(name="utcp", url=MANUAL_URL, allowed_query=())


def test_connect_utcp_rejects_a_string_allowed_query():
    with pytest.raises(UtcpConfigError):
        connect_utcp(MANUAL_URL, allow=["t"], transport=Fake(manual(tool())), allowed_query="format")


# --------------------------------------------------------------------- calls

def test_static_pairs_come_first_then_the_arguments():
    item = tool(url="https://api.example.com/o?format=utcp&mode=fast",
                inputs={"type": "object", "properties": {"zeta": {"type": "string"}, "alpha": {"type": "string"}}})
    fake, conn = connect(manual(item), allowed_query=["format", "mode"])
    conn.client.call_tool("utcp", "t", {"zeta": "1", "alpha": "2"}, True)
    assert fake.tool_calls[0][1] == "https://api.example.com/o?format=utcp&mode=fast&zeta=1&alpha=2"


def test_a_none_argument_named_like_a_static_parameter_is_ignored():
    fake, conn = connect(manual(tool()), allowed_query=["format"])
    conn.client.call_tool("utcp", "t", {"format": None, "id": "5"}, True)
    assert fake.tool_calls[0][1] == "https://api.example.com/tools/list_things/call?format=utcp&id=5"


def test_an_argument_named_like_a_static_parameter_is_refused_and_nothing_is_sent():
    # the toolkit's own schema check would stop this first, so go through the client: _build is the defence
    fake, conn = connect(manual(tool()), allowed_query=["format"])
    result = conn.client.call_tool("utcp", "t", {"format": "json", "id": "1"}, True)
    assert result.success is False and result.error == "UTCP tool arguments are not valid"
    assert fake.tool_calls == []
    assert conn.toolkit.call("utcp__t", json.dumps({"format": "json"})).success is False


# ----------------------------------------------- the real observed manual shape

def test_real_shape_with_token_and_allowed_query_is_offered_and_called():
    fake, conn = connect(manual(real_tool()), allow=["list_things"], token=TOKEN, allowed_query=["format"])
    assert [t.name for t in conn.tools] == ["utcp__list_things"]
    conn.client.call_tool("utcp", "list_things", {"arguments": {"limit": 2}}, True)
    method, url, headers, body = fake.tool_calls[0]
    assert (method, url) == ("POST", CALL_URL)
    assert headers["Authorization"] == "Bearer " + TOKEN and headers["Content-Type"] == "application/json"
    assert json.loads(body) == {"limit": 2}


def test_real_shape_with_a_token_but_no_allowed_query_is_stopped_by_the_query_rule():
    assert reason(manual(real_tool()), name="list_things", token=TOKEN) == OLD_QUERY


def test_real_shape_with_allowed_query_but_no_token_is_stopped_by_the_no_token_rule():
    assert reason(manual(real_tool()), name="list_things", allowed_query=["format"]) == NO_TOKEN


def test_a_large_schema_is_still_skipped():
    big = {"type": "object", "properties": {"arguments": {"type": "object", "description": "x" * 20000}}}
    item = real_tool(); item["inputs"] = big
    r = reason(manual(item), name="list_things", token=TOKEN, allowed_query=["format"])
    assert "schema" in r


def test_a_bare_question_mark_is_refused_even_without_allowed_query():
    assert reason(manual(tool(url="https://api.example.com/o?"))) == OLD_QUERY
