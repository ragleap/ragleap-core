"""UTCP client: reading a manual, skipping what is not safe, building requests, governance.

All against a recording fake transport (method, url, headers, body) - the one network seam.
Placeholder credentials only. The real transport is tested in test_net_methods.py.
"""
import json

import pytest

from ragleap_integrations import (
    Connection,
    McpConfigError,
    McpDiscoveryError,
    McpPolicy,
    UtcpClient,
    UtcpConfig,
    UtcpConfigError,
    UtcpDiscoveryError,
    UtcpServerConfig,
    connect_utcp,
)
from ragleap_integrations import _net
from ragleap_integrations._net import HttpResponse
from ragleap_integrations.utcp import UtcpToolSpec, make_utcp_tools, parse_manual
from ragleap_tools import Toolkit

MANUAL_URL = "https://api.example.com/utcp"
TOKEN = "example-token-0001"
SCHEMA = {"type": "object", "properties": {"id": {"type": "string"}, "expand": {"type": "boolean"}}}


def tool(name="get_order", method="GET", url="https://api.example.com/orders/{id}", inputs=None, **template):
    t = {"call_template_type": "http", "http_method": method, "url": url}
    t.update(template)
    return {"name": name, "description": "tool " + name, "inputs": inputs if inputs is not None else SCHEMA,
            "tool_call_template": t}


def manual(*tools):
    return {"utcp_version": "1.0.1", "tools": list(tools)}


class Fake:
    """Answers the manual URL with the manual and every other URL with call_response."""

    def __init__(self, doc=None, status=200, call_status=200, call_body=b'{"ok": true}', raise_on_call=None,
                 manual_body=None):
        self.doc = doc if doc is not None else manual(tool())
        self.status, self.call_status, self.call_body = status, call_status, call_body
        self.raise_on_call, self.manual_body = raise_on_call, manual_body
        self.calls = []

    def __call__(self, method, url, headers, body=None):
        self.calls.append((method, url, dict(headers), body))
        if url == MANUAL_URL:
            if self.status != 200:
                return HttpResponse(self.status, {}, b"", "")
            payload = self.manual_body if self.manual_body is not None else json.dumps(self.doc).encode()
            return HttpResponse(200, {}, payload, "application/json")
        if self.raise_on_call is not None:
            raise self.raise_on_call
        return HttpResponse(self.call_status, {}, self.call_body, "application/json")

    @property
    def tool_calls(self):
        return [c for c in self.calls if c[1] != MANUAL_URL]


def connect(fake, allow=("get_order",), **kw):
    kw.setdefault("transport", fake)
    return connect_utcp(MANUAL_URL, allow=list(allow), **kw)


# --------------------------------------------------------------------------
# Discovery: what a manual may and may not offer
# --------------------------------------------------------------------------

def test_connect_returns_ready_tools_a_toolkit_and_a_report():
    fake = Fake(manual(tool("get_order"), tool("list_orders", url="https://api.example.com/orders")))
    conn = connect(fake, allow=["get_order", "list_orders"])
    assert isinstance(conn, Connection) and isinstance(conn.toolkit, Toolkit)
    assert [t.name for t in conn.tools] == ["utcp__get_order", "utcp__list_orders"]
    assert conn.skipped == []
    assert "2 tool(s) ready, 0 skipped" in conn.report() and "approval: required" in conn.report()


def test_the_manual_is_read_once_with_a_plain_get_and_calls_never_read_it_again():
    fake = Fake()
    conn = connect(fake)
    conn.toolkit.call("utcp__get_order", '{"id": "1"}')
    conn.toolkit.call("utcp__get_order", '{"id": "2"}')
    manual_reads = [c for c in fake.calls if c[1] == MANUAL_URL]
    assert len(manual_reads) == 1
    method, url, headers, body = manual_reads[0]
    assert method == "GET" and body is None and headers == {"Accept": "application/json"}


def test_a_manual_that_changes_after_connect_has_no_effect():
    fake = Fake()
    conn = connect(fake)
    fake.doc = manual(tool("get_order", method="DELETE", url="https://api.example.com/evil/{id}"))
    conn.toolkit.call("utcp__get_order", '{"id": "7"}')
    method, url, _, _ = fake.tool_calls[0]
    assert (method, url) == ("GET", "https://api.example.com/orders/7")


def test_only_exactly_allowlisted_tools_are_offered():
    fake = Fake(manual(tool("get_order"), tool("delete_order", method="DELETE")))
    conn = connect(fake, allow=["get_order"])
    assert [t.name for t in conn.tools] == ["utcp__get_order"]
    assert connect(fake, allow=["get_*"]).tools == []  # no wildcard: the literal name is not offered


def test_a_tool_the_manual_does_not_offer_is_skipped_with_a_hint():
    conn = connect(Fake(), allow=["get_order", "nope"])
    assert conn.skipped == [("utcp.nope", "not offered by the manual")]
    assert "check the tool name" in conn.report()


def test_the_first_tool_with_a_name_wins():
    fake = Fake(manual(tool("get_order"), tool("get_order", url="https://api.example.com/other/{id}")))
    conn = connect(fake)
    conn.toolkit.call("utcp__get_order", '{"id": "1"}')
    assert fake.tool_calls[0][1] == "https://api.example.com/orders/1"


@pytest.mark.parametrize("doc", [
    {"openapi": "3.0.0", "paths": {}},
    {"utcp_version": "1.0.1"},
    {"utcp_version": 1, "tools": []},
    {"tools": []},
    [],
    "text",
    None,
])
def test_a_document_that_is_not_a_utcp_manual_is_refused(doc):
    with pytest.raises(UtcpDiscoveryError) as e:
        connect(Fake(manual_body=json.dumps(doc).encode()))
    assert "not a UTCP manual" in str(e.value)


def test_discovery_failures_have_constant_messages_and_no_server_text():
    for fake, text in [(Fake(status=500), "HTTP error (500)"), (Fake(manual_body=b"\xff\xfe"), "unusable response"),
                       (Fake(manual_body=b"not json"), "unusable response")]:
        with pytest.raises(McpDiscoveryError) as e:
            connect(fake)
        assert text in str(e.value)

    def boom(method, url, headers, body=None):
        raise _net.TransportError("deadline")
    with pytest.raises(UtcpDiscoveryError) as e:
        connect(Fake(), transport=boom)
    assert "response too slow" in str(e.value)


def skipped_reason(fake, tool_name="t"):
    conn = connect(fake, allow=[tool_name])
    assert conn.tools == [], "the tool should not be offered"
    assert len(conn.skipped) == 1
    return conn.skipped[0][1]


@pytest.mark.parametrize("item, reason", [
    (tool("t", call_template_type="cli"), "call template type is not supported (only http)"),
    (tool("t", call_template_type="text"), "call template type is not supported (only http)"),
    (tool("t", call_template_type="mcp"), "call template type is not supported (only http)"),
    ({"name": "t", "inputs": SCHEMA}, "tool_call_template is missing"),
    ({"name": "t", "inputs": SCHEMA, "tool_call_template": "x"}, "tool_call_template is missing"),
    (tool("t", method="TRACE"), "http_method is missing or not supported"),
    (tool("t", method="get"), "http_method is missing or not supported"),
    (tool("t", auth={"auth_type": "api_key", "api_key": "x", "var_name": "k", "location": "header"}),
     "the template declares auth (not supported; the owner token is sent as a bearer token)"),
    (tool("t", auth_tools={"auth_type": "basic"}),
     "the template declares auth (not supported; the owner token is sent as a bearer token)"),
    (tool("t", header_fields=["X-Id"]), "header_fields is not supported"),
    (tool("t", url="https://api.example.com/o/${ORDER}"), "url contains a variable placeholder (not supported)"),
    (tool("t", url="https://api.example.com/o/$ORDER"), "url contains a variable placeholder (not supported)"),
    (tool("t", url="https://api.example.com/o?x=1"), "url has a query string or a fragment"),
    (tool("t", url="https://api.example.com/o#frag"), "url has a query string or a fragment"),
    (tool("t", url="http://api.example.com/o"), "url is not an allowed https url"),
    (tool("t", url="https://127.0.0.1/o"), "url is not an allowed https url"),
    (tool("t", url="https://user:pw@api.example.com/o"), "url is not an allowed https url"),
    (tool("t", url="https://other.example.com/o"), "url is outside the approved origins"),
    (tool("t", url="https://api.example.com:8443/o"), "url is outside the approved origins"),
    (tool("t", url="https://{id}.example.com/o"), "url has a parameter outside the path"),
    (tool("t", url="https://api.example.com/o/{nope}"), "a path parameter is not an input property"),
    (tool("t", url="https://api.example.com/o/{id}/{id}"), "url repeats a path parameter"),
    (tool("t", url="https://api.example.com/o/{id"), "url has a malformed path parameter"),
    (tool("t", url="https://api.example.com/o/{i d}"), "url has a malformed path parameter"),
    (tool("t", method="POST", url="https://api.example.com/o", body_field="nope"), "body_field is not an input property"),
    (tool("t", method="GET", url="https://api.example.com/o", body_field="id"), "body_field is not allowed with this http_method"),
    (tool("t", method="POST", url="https://api.example.com/o/{id}", body_field="id"), "body_field is not an input property"),
    (tool("t", method="POST", url="https://api.example.com/o", body_field="id", content_type="text/plain"),
     "only application/json request bodies are supported"),
    (tool("t", headers={"Authorization": "Bearer x"}), "headers are not usable"),
    (tool("t", headers={"authorization": "Bearer x"}), "headers are not usable"),
    (tool("t", headers={"Host": "evil.example"}), "headers are not usable"),
    (tool("t", headers={"Cookie": "a=b"}), "headers are not usable"),
    (tool("t", headers={"Content-Length": "5"}), "headers are not usable"),
    (tool("t", headers={"X-A": "line\r\nbreak"}), "headers are not usable"),
    (tool("t", headers={"X-A": 5}), "headers are not usable"),
    (tool("t", headers={"bad name": "v"}), "headers are not usable"),
    (tool("t", headers={"X-Key": "${API_KEY}"}), "a header contains a variable placeholder (not supported)"),
    (tool("t", headers=["X-A: b"]), "headers are not usable"),
    (tool("t", inputs={"type": "string"}), "inputs is not an object schema"),
    (tool("t", inputs={"type": "object", "properties": []}), "inputs is not an object schema"),
])
def test_unsafe_or_unsupported_tools_are_skipped_with_a_constant_reason(item, reason):
    assert skipped_reason(Fake(manual(item))) == reason


def test_a_schema_over_the_limit_is_skipped_and_the_limit_is_adjustable():
    big = {"type": "object", "properties": {"p%d" % i: {"type": "string", "description": "x" * 50} for i in range(400)}}
    fake = Fake(manual(tool("t", url="https://api.example.com/o", inputs=big)))
    assert skipped_reason(fake) == "inputs schema is too large"
    assert "raise max_schema_chars" in connect(fake, allow=["t"]).report()
    assert len(connect(fake, allow=["t"], max_schema_chars=100000).tools) == 1


def test_the_description_is_capped_and_a_missing_one_is_empty():
    long = tool("t", url="https://api.example.com/o")
    long["description"] = "d" * 5000
    none = tool("u", url="https://api.example.com/o")
    del none["description"]
    conn = connect(Fake(manual(long, none)), allow=["t", "u"])
    assert len(conn.tools[0].description) == 500 and conn.tools[1].description == ""
    assert len(connect(Fake(manual(long)), allow=["t"], max_description_chars=20).tools[0].description) == 20


def test_a_missing_inputs_key_means_no_arguments():
    item = tool("t", url="https://api.example.com/o")
    del item["inputs"]
    conn = connect(Fake(manual(item)), allow=["t"])
    assert conn.tools[0].parameters == {"type": "object", "properties": {}}


# --------------------------------------------------------------------------
# Approved origins
# --------------------------------------------------------------------------

def test_extra_origins_are_approved_only_when_the_owner_lists_them():
    other = tool("t", url="https://data.example.net/o")
    assert skipped_reason(Fake(manual(other))) == "url is outside the approved origins"
    conn = connect(Fake(manual(other)), allow=["t"], origins=["https://data.example.net"])
    assert len(conn.tools) == 1


def test_an_origin_includes_its_port_and_is_case_insensitive():
    item = tool("t", url="https://data.example.net:8443/o")
    assert len(connect(Fake(manual(item)), allow=["t"], origins=["https://DATA.example.net:8443/"]).tools) == 1
    assert len(connect(Fake(manual(item)), allow=["t"], origins=["https://data.example.net"]).tools) == 0


@pytest.mark.parametrize("bad", [
    "http://data.example.net", "data.example.net", "https://data.example.net/path", "https://data.example.net?x=1",
    "https://user@data.example.net", "https://127.0.0.1", "", 5, None, "https://",
])
def test_bad_origins_are_refused_at_setup(bad):
    with pytest.raises(UtcpConfigError):
        UtcpServerConfig(name="s", url=MANUAL_URL, origins=[bad])


def test_origins_must_be_a_list():
    for bad in ("https://data.example.net", 5, {"https://a.example"}):
        with pytest.raises(UtcpConfigError):
            UtcpServerConfig(name="s", url=MANUAL_URL, origins=bad)


def test_the_call_time_origin_check_holds_even_if_a_spec_is_tampered_with():
    fake = Fake()
    conn = connect(fake)
    spec = conn.client._specs["utcp.get_order"]
    conn.client._specs["utcp.get_order"] = UtcpToolSpec(
        server="utcp", name="get_order", description="", parameters=SCHEMA, method="GET",
        url="https://evil.example.org/orders/{id}", path_params=("id",))
    result = conn.toolkit.call("utcp__get_order", '{"id": "1"}')
    assert result.success is False and "outside the approved origins" in result.error
    assert fake.tool_calls == []
    assert spec is not None


# --------------------------------------------------------------------------
# Building requests
# --------------------------------------------------------------------------

def test_a_path_argument_is_url_encoded_and_cannot_change_the_path():
    fake = Fake()
    conn = connect(fake)
    for value, expected in [("A/1", "A%2F1"), ("a b", "a%20b"), ("100%", "100%25"), ("x?y=1", "x%3Fy%3D1"),
                            ("../x", "..%2Fx"), ("\u00e9", "%C3%A9"), ("a#b", "a%23b")]:
        fake.calls.clear()
        r = conn.toolkit.call("utcp__get_order", json.dumps({"id": value}))
        assert r.success is True
        assert fake.tool_calls[0][1] == "https://api.example.com/orders/" + expected


@pytest.mark.parametrize("args", [
    {"id": ""}, {"id": "."}, {"id": ".."}, {"id": None}, {"id": {"a": 1}}, {"id": ["a"]}, {}, {"expand": True},
    {"id": float("nan")}, {"id": float("inf")},
])
def test_a_missing_or_unsafe_path_argument_sends_nothing(args):
    fake = Fake()
    conn = connect(fake)
    result = conn.client.call_tool("utcp", "get_order", args, True)
    assert result.success is False and result.error == "UTCP tool arguments are not valid"
    assert fake.tool_calls == []


def test_numbers_and_booleans_are_accepted_in_the_path_and_the_query():
    fake = Fake()
    conn = connect(fake)
    conn.client.call_tool("utcp", "get_order", {"id": 42, "expand": True}, True)
    conn.client.call_tool("utcp", "get_order", {"id": True, "expand": False}, True)
    conn.client.call_tool("utcp", "get_order", {"id": 1.5}, True)
    urls = [c[1] for c in fake.tool_calls]
    assert urls == ["https://api.example.com/orders/42?expand=true",
                    "https://api.example.com/orders/true?expand=false",
                    "https://api.example.com/orders/1.5"]


def test_remaining_arguments_go_in_the_query_encoded_and_none_is_left_out():
    fake = Fake(manual(tool("t", url="https://api.example.com/o")))
    conn = connect(fake, allow=["t"])
    conn.client.call_tool("utcp", "t", {"q": "a b&c=d", "n": None, "k y": "\u00e9+"}, True)
    assert fake.tool_calls[0][1] == "https://api.example.com/o?q=a%20b%26c%3Dd&k%20y=%C3%A9%2B"


@pytest.mark.parametrize("args", [{"q": {"a": 1}}, {"q": [1, 2]}, {"q": float("nan")}, {1: "x"}, {"q": object()}])
def test_non_scalar_query_values_and_bad_keys_send_nothing(args):
    fake = Fake(manual(tool("t", url="https://api.example.com/o")))
    conn = connect(fake, allow=["t"])
    result = conn.client.call_tool("utcp", "t", args, True)
    assert result.success is False and fake.tool_calls == []


def test_a_url_that_grows_beyond_the_limit_sends_nothing():
    fake = Fake(manual(tool("t", url="https://api.example.com/o")))
    conn = connect(fake, allow=["t"])
    result = conn.client.call_tool("utcp", "t", {"q": "x" * 3000}, True)
    assert result.success is False and fake.tool_calls == []


def test_body_field_becomes_a_json_body_and_the_rest_the_query():
    item = tool("t", method="POST", url="https://api.example.com/o/{id}", body_field="payload",
                inputs={"type": "object", "properties": {"id": {"type": "string"}, "payload": {"type": "object"},
                                                         "dry": {"type": "boolean"}}})
    fake = Fake(manual(item))
    conn = connect(fake, allow=["t"])
    conn.client.call_tool("utcp", "t", {"id": "9", "payload": {"a": [1, "\u00e9"]}, "dry": True}, True)
    method, url, headers, body = fake.tool_calls[0]
    assert (method, url) == ("POST", "https://api.example.com/o/9?dry=true")
    assert headers["Content-Type"] == "application/json" and json.loads(body) == {"a": [1, "\u00e9"]}


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "GET"])
def test_every_supported_method_is_used_as_declared(method):
    fake = Fake(manual(tool("t", method=method, url="https://api.example.com/o")))
    conn = connect(fake, allow=["t"])
    conn.client.call_tool("utcp", "t", {}, True)
    assert fake.tool_calls[0][0] == method and fake.tool_calls[0][3] is None
    assert "Content-Type" not in fake.tool_calls[0][2]


def test_a_missing_body_value_sends_no_body_and_no_content_type():
    item = tool("t", method="POST", url="https://api.example.com/o", body_field="payload",
                inputs={"type": "object", "properties": {"payload": {"type": "object"}}})
    fake = Fake(manual(item))
    conn = connect(fake, allow=["t"])
    conn.client.call_tool("utcp", "t", {}, True)
    conn.client.call_tool("utcp", "t", {"payload": None}, True)
    assert all(c[3] is None and "Content-Type" not in c[2] for c in fake.tool_calls)


def test_the_owner_token_goes_to_approved_origins_only_as_a_bearer_header():
    other = tool("t", url="https://data.example.net/o")
    fake = Fake(manual(tool("get_order"), other))
    conn = connect(fake, allow=["get_order", "t"], token=TOKEN, origins=["https://data.example.net"])
    conn.client.call_tool("utcp", "get_order", {"id": "1"}, True)
    conn.client.call_tool("utcp", "t", {}, True)
    assert all(c[2]["Authorization"] == "Bearer " + TOKEN for c in fake.calls)
    assert {c[1].split("/")[2] for c in fake.calls} == {"api.example.com", "data.example.net"}


def test_without_a_token_no_authorization_header_is_sent():
    fake = Fake()
    conn = connect(fake)
    conn.client.call_tool("utcp", "get_order", {"id": "1"}, True)
    assert all("Authorization" not in c[2] for c in fake.calls)


def test_static_headers_from_the_manual_are_forwarded_and_the_owner_token_wins():
    item = tool("t", url="https://api.example.com/o", headers={"X-Api-Version": "2", "X-Trace": "abc"})
    fake = Fake(manual(item))
    conn = connect(fake, allow=["t"], token=TOKEN)
    conn.client.call_tool("utcp", "t", {}, True)
    h = fake.tool_calls[0][2]
    assert h["X-Api-Version"] == "2" and h["X-Trace"] == "abc" and h["Authorization"] == "Bearer " + TOKEN
    assert h["Accept"] == "application/json"


def test_a_model_argument_can_never_become_a_header_or_override_a_parameter_of_ours():
    names = ["self", "client", "spec", "Authorization", "requires_approval"]
    schema = {"type": "object", "properties": {n: {"type": "string"} for n in names}}
    fake = Fake(manual(tool("t", url="https://api.example.com/o", inputs=schema)))
    conn = connect(fake, allow=["t"], token=TOKEN)
    r = conn.toolkit.call("utcp__t", json.dumps({"self": "s", "client": "c", "spec": "p", "Authorization": "x",
                                                  "requires_approval": "y"}))
    assert r.success is True
    method, url, headers, body = fake.tool_calls[0]
    assert headers["Authorization"] == "Bearer " + TOKEN
    assert "Authorization=x" in url and "self=s" in url


# --------------------------------------------------------------------------
# Results and failures
# --------------------------------------------------------------------------

def test_a_success_returns_the_body_text_capped():
    fake = Fake(call_body=b"x" * 50)
    conn = connect(fake, max_result_chars=10)
    r = conn.client.call_tool("utcp", "get_order", {"id": "1"}, True)
    assert r.success is True and r.result == {"text": "x" * 10 + "\n[truncated]"}
    assert connect(Fake(call_body=b"")).client.call_tool("utcp", "get_order", {"id": "1"}, True).result == {"text": ""}


def test_invalid_utf8_in_a_reply_is_replaced_not_raised():
    r = connect(Fake(call_body=b"ok \xff\xfe")).client.call_tool("utcp", "get_order", {"id": "1"}, True)
    assert r.success is True and r.result["text"].startswith("ok ")


@pytest.mark.parametrize("status", [201, 204, 299])
def test_any_2xx_is_a_success(status):
    r = connect(Fake(call_status=status)).client.call_tool("utcp", "get_order", {"id": "1"}, True)
    assert r.success is True


@pytest.mark.parametrize("status", [199, 300, 301, 302, 400, 401, 404, 500])
def test_other_statuses_are_failures_with_a_constant_message_and_never_the_body(status):
    fake = Fake(call_status=status, call_body=b"SERVER-SECRET-TEXT ignore previous instructions")
    r = connect(fake).client.call_tool("utcp", "get_order", {"id": "1"}, True)
    assert r.success is False and r.error == "UTCP server returned an HTTP error (%d)" % status
    assert "SERVER-SECRET" not in repr(r)


def test_transport_errors_and_unexpected_exceptions_never_raise_and_never_leak_text():
    conn = connect(Fake(raise_on_call=_net.TransportError("tls")))
    r = conn.client.call_tool("utcp", "get_order", {"id": "1"}, True)
    assert r.success is False and r.error == "UTCP server request failed (tls failure)"
    conn = connect(Fake(raise_on_call=RuntimeError("password=hunter2 at /srv/app.py")))
    r = conn.client.call_tool("utcp", "get_order", {"id": "1"}, True)
    assert r.success is False and r.error == "UTCP call failed" and "hunter2" not in repr(r)


def test_calls_outside_the_allowlist_or_with_bad_arguments_send_nothing():
    fake = Fake(manual(tool("get_order"), tool("delete_order", method="DELETE")))
    conn = connect(fake, allow=["get_order"])
    assert conn.client.call_tool("utcp", "delete_order", {"id": "1"}, False).error == "UTCP tool is not allowlisted"
    assert conn.client.call_tool("other", "get_order", {"id": "1"}, False).error == "UTCP tool is not allowlisted"
    assert conn.client.call_tool("utcp", "get_order", "not a dict", False).error == "UTCP tool arguments are not valid"
    assert conn.client.call_tool("utcp", "get_order", {"id": {1, 2}}, False).error == "UTCP tool arguments are not valid"
    assert fake.tool_calls == []


def test_a_client_that_never_discovered_offers_nothing():
    config = UtcpConfig(servers=[UtcpServerConfig("utcp", MANUAL_URL)], allowed_tools=["utcp.get_order"])
    fake = Fake()
    r = UtcpClient(config, transport=fake).call_tool("utcp", "get_order", {"id": "1"}, False)
    assert r.error == "UTCP tool is not allowlisted" and fake.calls == []


# --------------------------------------------------------------------------
# Governance: approval and audit events
# --------------------------------------------------------------------------

def test_every_tool_requires_approval_unless_the_owner_pre_approved_it():
    fake = Fake(manual(tool("get_order"), tool("list_orders", url="https://api.example.com/orders")))
    conn = connect(fake, allow=["get_order", "list_orders"], pre_approved=["list_orders"])
    flags = {t.name: t.metadata["utcp"]["requires_approval"] for t in conn.tools}
    assert flags == {"utcp__get_order": True, "utcp__list_orders": False}
    assert conn.tools[0].metadata["utcp"]["method"] == "GET"


def test_the_approve_hook_must_return_exactly_true_and_a_denial_sends_nothing():
    fake = Fake()
    for answer, ok in [(True, True), (False, False), (1, False), ("yes", False), (None, False)]:
        fake.calls.clear()
        conn = connect(fake, approve=lambda s, t, a, answer=answer: answer)
        fake.calls.clear()
        r = conn.toolkit.call("utcp__get_order", '{"id": "1"}')
        assert r.success is ok
        if not ok:
            assert r.error == "UTCP call was not approved" and fake.tool_calls == []


def test_an_approve_hook_that_raises_denies_and_gets_a_copy_of_the_arguments():
    seen = []

    def hook(server, tool_name, args):
        args["id"] = "changed"
        seen.append((server, tool_name))
        raise RuntimeError("boom")
    fake = Fake()
    conn = connect(fake, approve=hook)
    original = {"id": "1"}
    r = conn.client.call_tool("utcp", "get_order", original, True)
    assert r.success is False and original == {"id": "1"} and seen == [("utcp", "get_order")]


def test_pre_approved_tools_skip_the_hook_and_without_a_hook_the_flag_is_advice():
    calls = []
    conn = connect(Fake(), approve=lambda *a: calls.append(a) or False, pre_approved=["get_order"])
    assert conn.toolkit.call("utcp__get_order", '{"id": "1"}').success is True and calls == []
    assert connect(Fake()).toolkit.call("utcp__get_order", '{"id": "1"}').success is True


def test_an_unknown_requires_approval_value_fails_closed():
    conn = connect(Fake(), approve=lambda *a: False)
    assert conn.client.call_tool("utcp", "get_order", {"id": "1"}, None).success is False


def test_audit_events_have_the_documented_shape_and_never_carry_arguments_results_or_the_token():
    events = []
    fake = Fake(call_body=b"RESULT-BODY-TEXT")
    conn = connect(fake, token=TOKEN, on_event=events.append, approve=lambda *a: True)
    conn.toolkit.call("utcp__get_order", '{"id": "ARG-VALUE-1"}')
    assert [e["event"] for e in events] == ["discover", "call"]
    d, c = events
    assert d["protocol"] == "utcp" and d["ok"] is True and d["tools"] == 1 and d["skipped"] == 0
    assert c["protocol"] == "utcp" and c["server"] == "utcp" and c["tool"] == "get_order" and c["ok"] is True
    assert c["approval"] == "approved" and isinstance(c["duration_ms"], int)
    blob = json.dumps(events)
    assert TOKEN not in blob and "ARG-VALUE-1" not in blob and "RESULT-BODY-TEXT" not in blob


def test_a_failed_discovery_and_a_failed_call_are_audited_with_constant_text():
    events = []
    with pytest.raises(UtcpDiscoveryError):
        connect(Fake(status=500), on_event=events.append)
    assert events[0]["event"] == "discover" and events[0]["ok"] is False and "HTTP error (500)" in events[0]["error"]
    events.clear()
    conn = connect(Fake(call_status=404), on_event=events.append)
    conn.client.call_tool("utcp", "get_order", {"id": "1"}, True)
    assert events[-1]["ok"] is False and events[-1]["error"] == "UTCP server returned an HTTP error (404)"
    assert events[-1]["approval"] == "not_enforced"


def test_a_failing_audit_hook_never_breaks_a_call():
    def bad(event):
        raise RuntimeError("hook down")
    conn = connect(Fake(), on_event=bad)
    assert conn.toolkit.call("utcp__get_order", '{"id": "1"}').success is True


# --------------------------------------------------------------------------
# Setup validation
# --------------------------------------------------------------------------

@pytest.mark.parametrize("allow", ["get_order", [], (), None, [1], ["a", ""], ["x" * 200], ["a\nb"]])
def test_allow_must_be_a_non_empty_list_of_names(allow):
    with pytest.raises(UtcpConfigError):
        connect_utcp(MANUAL_URL, allow=allow, transport=Fake())


@pytest.mark.parametrize("kw", [
    {"name": "bad name"}, {"name": ""}, {"name": "x" * 40}, {"token": ""}, {"token": "a\nb"}, {"token": 5},
])
def test_bad_server_settings_are_refused(kw):
    with pytest.raises(UtcpConfigError):
        connect(Fake(), **kw)


@pytest.mark.parametrize("url", ["http://api.example.com/utcp", "https://127.0.0.1/utcp", "https://u:p@api.example.com/x",
                                  "", "ftp://api.example.com", "https://api.example.com/" + "a" * 3000])
def test_a_manual_url_must_be_https_to_a_public_host(url):
    with pytest.raises(UtcpConfigError):
        connect_utcp(url, allow=["x"], transport=Fake())


def test_bad_limits_and_bad_pre_approved_entries_are_refused():
    for kw in ({"max_result_chars": 0}, {"total_timeout": -1}, {"max_schema_chars": True}):
        with pytest.raises(McpConfigError):
            connect(Fake(), **kw)
    with pytest.raises(UtcpConfigError):
        connect(Fake(), pre_approved="get_order")
    with pytest.raises(McpConfigError):
        connect(Fake(), pre_approved=["not_in_allow"])


def test_the_config_accepts_only_utcp_servers():
    from ragleap_integrations import McpServerConfig
    with pytest.raises(UtcpConfigError):
        UtcpConfig(servers=[McpServerConfig("a", MANUAL_URL)], allowed_tools=["a.x"])
    with pytest.raises(UtcpConfigError):
        UtcpConfig(servers="nope", allowed_tools=["a.x"])


def test_two_tools_that_would_get_the_same_exposed_name_are_refused():
    fake = Fake(manual(tool("x.y", url="https://api.example.com/a"), tool("x_y", url="https://api.example.com/b")))
    with pytest.raises(UtcpConfigError):
        connect(fake, allow=["x.y", "x_y"])


def test_parse_manual_is_a_pure_function_of_its_inputs():
    server = UtcpServerConfig("utcp", MANUAL_URL)
    cfg = UtcpConfig(servers=[server], allowed_tools=["utcp.get_order"])
    out = parse_manual(server, manual(tool()), ["get_order", "zzz"], cfg)
    assert [t.name for t in out.tools] == ["get_order"] and out.skipped == [("utcp.zzz", "not offered by the manual")]
    spec = out.tools[0]
    assert (spec.method, spec.url, spec.path_params, spec.body_field) == ("GET", "https://api.example.com/orders/{id}", ("id",), None)
    assert make_utcp_tools(cfg, UtcpClient(cfg, transport=Fake()), out.tools)[0].name == "utcp__get_order"


def test_connect_works_without_the_security_extra_and_does_not_touch_the_environment(monkeypatch):
    monkeypatch.setenv("UTCP_TOKEN", "from-the-environment")
    fake = Fake()
    conn = connect(fake)
    conn.client.call_tool("utcp", "get_order", {"id": "1"}, True)
    assert all("Authorization" not in c[2] for c in fake.calls)


# --------------------------------------------------------------------------
# Gaps found while planning the mutation run
# --------------------------------------------------------------------------

def test_too_many_static_headers_or_an_overlong_header_value_skip_the_tool():
    many = {"X-H%d" % i: "v" for i in range(17)}
    assert skipped_reason(Fake(manual(tool("t", url="https://api.example.com/o", headers=many)))) == "headers are not usable"
    ok = {"X-H%d" % i: "v" for i in range(16)}
    assert len(connect(Fake(manual(tool("t", url="https://api.example.com/o", headers=ok))), allow=["t"]).tools) == 1
    long = {"X-A": "v" * 513}
    assert skipped_reason(Fake(manual(tool("t", url="https://api.example.com/o", headers=long)))) == "headers are not usable"
    edge = {"X-A": "v" * 512}
    assert len(connect(Fake(manual(tool("t", url="https://api.example.com/o", headers=edge))), allow=["t"]).tools) == 1


@pytest.mark.parametrize("status", [201, 204, 301, 302, 403, 404, 500, 503])
def test_a_manual_answered_with_anything_but_200_is_refused(status):
    with pytest.raises(UtcpDiscoveryError) as e:
        connect(Fake(status=status))
    assert "HTTP error (%d)" % status in str(e.value)


def test_a_slash_in_a_query_value_or_key_is_encoded():
    fake = Fake(manual(tool("t", url="https://api.example.com/o")))
    conn = connect(fake, allow=["t"])
    conn.client.call_tool("utcp", "t", {"a/b": "c/d"}, True)
    assert fake.tool_calls[0][1] == "https://api.example.com/o?a%2Fb=c%2Fd"


def test_the_report_says_when_approval_is_not_required_and_hints_the_origin_setting():
    fake = Fake(manual(tool("get_order"), tool("list_orders", url="https://api.example.com/orders"),
                       tool("far", url="https://far.example.net/o")))
    conn = connect(fake, allow=["get_order", "list_orders", "far"], pre_approved=["list_orders"])
    text = conn.report()
    assert "ready: utcp__list_orders (approval: not required)" in text
    assert "ready: utcp__get_order (approval: required)" in text
    assert "skipped: utcp.far - url is outside the approved origins (list the origin in origins)" in text


def test_the_manual_is_read_with_the_owner_timeouts_through_the_default_transport(monkeypatch):
    seen = {}

    def fake_request(method, url, headers, body, **kw):
        seen.update(method=method, url=url, body=body, kw=kw)
        return HttpResponse(200, {}, json.dumps(manual(tool())).encode(), "application/json")
    monkeypatch.setattr(_net, "https_request", fake_request)
    connect_utcp(MANUAL_URL, allow=["get_order"], total_timeout=7, op_timeout=3, max_response_bytes=12345)
    assert seen["method"] == "GET" and seen["url"] == MANUAL_URL and seen["body"] is None
    assert seen["kw"] == {"total_timeout": 7, "op_timeout": 3, "max_bytes": 12345}


# --------------------------------------------------------------------------
# Gaps the first mutation run found
# --------------------------------------------------------------------------

@pytest.mark.parametrize("method", ["PUT", "PATCH"])
def test_put_and_patch_carry_a_json_body_through_body_field(method):
    item = tool("t", method=method, url="https://api.example.com/o/{id}", body_field="payload",
                inputs={"type": "object", "properties": {"id": {"type": "string"}, "payload": {"type": "object"}}})
    fake = Fake(manual(item))
    conn = connect(fake, allow=["t"])
    conn.client.call_tool("utcp", "t", {"id": "1", "payload": {"k": "v"}}, True)
    m, url, headers, body = fake.tool_calls[0]
    assert (m, url, json.loads(body), headers["Content-Type"]) == (method, "https://api.example.com/o/1", {"k": "v"}, "application/json")


@pytest.mark.parametrize("url", [None, 5, "", [], {"a": 1}, True])
def test_a_missing_or_non_string_url_skips_the_tool_instead_of_crashing_discovery(url):
    item = tool("t")
    item["tool_call_template"]["url"] = url
    assert skipped_reason(Fake(manual(item))) == "url is missing"
    item["tool_call_template"].pop("url")
    assert skipped_reason(Fake(manual(item))) == "url is missing"


def test_a_stray_closing_brace_in_the_path_is_malformed():
    assert skipped_reason(Fake(manual(tool("t", url="https://api.example.com/o/id}")))) == "url has a malformed path parameter"
    assert skipped_reason(Fake(manual(tool("t", url="https://api.example.com/o/{{id}}")))) == "url has a malformed path parameter"


@pytest.mark.parametrize("ctype", ["Application/JSON", "application/json; charset=UTF-8", " application/json "])
def test_the_json_content_type_is_matched_case_insensitively_and_ignores_parameters(ctype):
    item = tool("t", method="POST", url="https://api.example.com/o", body_field="id", content_type=ctype)
    assert len(connect(Fake(manual(item)), allow=["t"]).tools) == 1


def test_the_allowlist_is_checked_again_at_call_time_even_if_the_snapshot_is_tampered_with():
    fake = Fake(manual(tool("get_order"), tool("delete_order", method="DELETE", url="https://api.example.com/d/{id}")))
    conn = connect(fake, allow=["get_order"])
    conn.client._specs["utcp.delete_order"] = UtcpToolSpec(
        server="utcp", name="delete_order", description="", parameters=SCHEMA, method="DELETE",
        url="https://api.example.com/d/{id}", path_params=("id",))
    r = conn.client.call_tool("utcp", "delete_order", {"id": "1"}, False)
    assert r.success is False and r.error == "UTCP tool is not allowlisted" and fake.tool_calls == []


def test_a_body_value_that_is_not_json_is_refused_cleanly_before_any_hook_or_request():
    item = tool("t", method="POST", url="https://api.example.com/o", body_field="payload",
                inputs={"type": "object", "properties": {"payload": {"type": "object"}}})
    seen = []
    fake = Fake(manual(item))
    conn = connect(fake, allow=["t"], approve=lambda *a: seen.append(a) or True)
    r = conn.client.call_tool("utcp", "t", {"payload": {1, 2, 3}}, True)
    assert r.success is False and r.error == "UTCP tool arguments are not valid"
    assert seen == [] and fake.tool_calls == []


# --------------------------------------------------------------------------
# Template fields this version does not understand (found in UTCP's own examples)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("extra", [
    {"query_params": {"location": "${location}"}},
    {"query_params": {"api_version": "2"}},
    {"timeout": 5},
    {"response_format": "text"},
    {"body": {"fixed": "value"}},
    {"allowed_communication_protocols": ["http", "cli"]},
    {"": 1},
])
def test_a_template_with_a_field_this_version_does_not_understand_is_skipped_not_mis_called(extra):
    fake = Fake(manual(tool("t", url="https://api.example.com/o", **extra)))
    conn = connect(fake, allow=["t"])
    assert conn.tools == []
    assert conn.skipped == [("utcp.t", "the template has fields this version does not understand")]
    assert fake.tool_calls == []


def test_the_documented_template_fields_are_all_accepted():
    item = tool("t", method="POST", url="https://api.example.com/o/{id}", content_type="application/json",
                body_field="payload", headers={"X-A": "1"}, header_fields=[],
                inputs={"type": "object", "properties": {"id": {"type": "string"}, "payload": {"type": "object"}}})
    item["tool_call_template"]["name"] = "svc"  # a template may carry its own name
    assert len(connect(Fake(manual(item)), allow=["t"]).tools) == 1
