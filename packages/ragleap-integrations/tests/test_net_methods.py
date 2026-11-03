"""https_request(): GET, PUT, PATCH and DELETE (new in 0.4.0) against a real local TLS
server with a throwaway certificate, using the helpers of test_net.py. The properties
that matter are the same as for POST: IP pinning, verified TLS, no redirects, a size cap
and constant errors - plus the new ones: only five methods, and what each one puts on
the wire."""
import pytest

from ragleap_integrations import _net
from ragleap_integrations._net import ALLOWED_METHODS, TransportError, https_post, https_request
from test_net import PUBLIC_IP, URL, Client, make_server, ok_handler, read_request, respond, tls_material  # noqa: F401


def request_line_and_headers(head):
    lines = head.decode().split("\r\n")
    h = {l.split(":", 1)[0].lower(): l.split(":", 1)[1].strip() for l in lines[1:] if ":" in l}
    return lines[0], h


def test_the_allowed_methods_are_exactly_the_five_utcp_uses():
    assert ALLOWED_METHODS == ("GET", "POST", "PUT", "DELETE", "PATCH")


@pytest.mark.parametrize("method", ["GET", "DELETE"])
def test_get_and_delete_send_no_body_and_no_content_length(method, make_server, tls_material):
    srv = make_server(ok_handler)
    cl = Client(srv, tls_material)
    resp = https_request(method, "https://mcp.example.com/orders/7?x=1", {"X-Test": "1"}, None, **cl.kwargs())
    assert resp.status == 200 and resp.body == b'{"ok": true}'
    head, body = srv.requests[0]
    line, h = request_line_and_headers(head)
    assert line == method + " /orders/7?x=1 HTTP/1.1"
    assert body == b"" and "content-length" not in h and "transfer-encoding" not in h
    assert h["host"] == "mcp.example.com" and h["x-test"] == "1"
    assert h["accept-encoding"] == "identity" and h["connection"] == "close"
    assert cl.connects == [(PUBLIC_IP, 443)]


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH"])
def test_post_put_and_patch_send_the_body_with_its_exact_length(method, make_server, tls_material):
    srv = make_server(ok_handler)
    cl = Client(srv, tls_material)
    payload = '{"a": "\u00e9"}'.encode("utf-8")
    https_request(method, "https://mcp.example.com/o", {"Content-Type": "application/json"}, payload, **cl.kwargs())
    head, body = srv.requests[0]
    line, h = request_line_and_headers(head)
    assert line == method + " /o HTTP/1.1"
    assert h["content-length"] == str(len(payload)) and body == payload


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH"])
def test_a_body_less_post_put_or_patch_says_content_length_zero(method, make_server, tls_material):
    srv = make_server(ok_handler)
    cl = Client(srv, tls_material)
    https_request(method, "https://mcp.example.com/o", {}, None, **cl.kwargs())
    _, h = request_line_and_headers(srv.requests[0][0])
    assert h["content-length"] == "0"


@pytest.mark.parametrize("method", ["TRACE", "CONNECT", "OPTIONS", "HEAD", "get", "Get", "", None, 5, "GET ", "GET\r\nX: y"])
def test_any_other_method_is_refused_before_dns_or_connect(method, make_server, tls_material):
    srv = make_server(ok_handler)
    cl = Client(srv, tls_material)
    resolved = []

    def resolver(host, port, type=0):
        resolved.append(host)
        raise AssertionError("must not resolve")
    with pytest.raises(TransportError) as e:
        https_request(method, URL, {}, None, **cl.kwargs(resolver=resolver))
    assert e.value.code == "method" and e.value.message == "http method not allowed"
    assert resolved == [] and cl.connects == [] and srv.requests == []


def test_the_method_is_checked_before_the_url_so_a_bad_pair_reports_the_method():
    with pytest.raises(TransportError) as e:
        https_request("TRACE", "http://insecure.example.com/", {}, None)
    assert e.value.code == "method"
    with pytest.raises(TransportError) as e:
        https_request("GET", "http://insecure.example.com/", {}, None)
    assert e.value.code == "scheme"


@pytest.mark.parametrize("method", ["GET", "PUT", "PATCH", "DELETE"])
def test_a_redirect_is_returned_never_followed_for_every_method(method, make_server, tls_material):
    def handler(server, conn):
        head, body = read_request(conn)
        server.requests.append((head, body))
        respond(conn, 302, b"", extra="Location: https://internal.example.org/secret\r\n")
    srv = make_server(handler)
    cl = Client(srv, tls_material)
    resp = https_request(method, "https://mcp.example.com/o", {}, None, **cl.kwargs())
    assert resp.status == 302 and resp.headers["location"] == "https://internal.example.org/secret"
    assert len(srv.requests) == 1 and cl.connects == [(PUBLIC_IP, 443)]


def test_the_size_cap_and_the_error_status_apply_to_a_get(make_server, tls_material):
    def big(server, conn):
        read_request(conn)
        respond(conn, 200, b"x" * 5000)
    cl = Client(make_server(big), tls_material)
    with pytest.raises(TransportError) as e:
        https_request("GET", URL, {}, None, **cl.kwargs(max_bytes=1000))
    assert e.value.code == "too_large"

    def notfound(server, conn):
        read_request(conn)
        respond(conn, 404, b"SERVER TEXT")
    cl = Client(make_server(notfound), tls_material)
    resp = https_request("GET", URL, {}, None, **cl.kwargs())
    assert resp.status == 404 and resp.body == b"SERVER TEXT"


def test_a_non_public_address_is_refused_for_a_get_before_any_connect(make_server, tls_material):
    from test_net import _resolver
    srv = make_server(ok_handler)
    cl = Client(srv, tls_material)
    with pytest.raises(TransportError) as e:
        https_request("GET", URL, {}, None, **cl.kwargs(resolver=_resolver("10.0.0.5")))
    assert e.value.code == "non_public" and cl.connects == []


def test_hostname_mismatch_still_fails_for_a_get(make_server, tls_material):
    srv = make_server(ok_handler)
    cl = Client(srv, tls_material)
    with pytest.raises(TransportError) as e:
        https_request("GET", "https://other.example.com/", {}, None, **cl.kwargs())
    assert e.value.code == "tls"


def test_https_post_is_a_post_through_https_request_with_the_same_options(make_server, tls_material):
    srv = make_server(ok_handler)
    cl = Client(srv, tls_material)
    resp = https_post(URL, {"Content-Type": "application/json"}, b"{}", **cl.kwargs(max_bytes=100))
    assert resp.status == 200
    line, h = request_line_and_headers(srv.requests[0][0])
    assert line == "POST /mcp HTTP/1.1" and h["content-length"] == "2"
    assert _net.https_post.__name__ == "https_post"


def test_default_headers_cannot_be_dropped_by_the_caller_but_can_be_added(make_server, tls_material):
    srv = make_server(ok_handler)
    cl = Client(srv, tls_material)
    https_request("GET", URL, {"Accept": "application/json"}, None, **cl.kwargs())
    _, h = request_line_and_headers(srv.requests[0][0])
    assert h["accept"] == "application/json" and h["user-agent"].startswith("ragleap-integrations/")
