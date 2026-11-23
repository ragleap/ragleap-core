"""ragleap_security.net (design: issue #1506). The real address rule, URL parser and redirect logic run in every test;
only the DNS answer and the connection target go through the documented test seam, because loopback is blocked."""
import gzip
import json
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from ragleap_security import AuditLog, RateLimiter, RateLimitError, Scope, ScopeError
from ragleap_security.net import (REASONS, FetchPolicy, FetchResult, NetError, SafeFetcher, _host_allowed, _parse,
                                  address_allowed)

A, B = Scope("acme"), Scope("globex")
PUBLIC = "93.184.216.34"


class Server:
    def __init__(self):
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                pass

            def do_GET(self):
                self.serve()

            def do_HEAD(self):
                self.serve()

            def serve(self):
                u = urlsplit(self.path)
                q = parse_qs(u.query)
                outer.requests.append((self.command, self.path, {k.lower(): v for k, v in self.headers.items()}))
                try:
                    if u.path == "/redir":
                        self.send_response(int(q.get("code", ["302"])[0]))
                        self.send_header("Location", q["to"][0])
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                    elif u.path == "/loop":
                        self.send_response(302)
                        self.send_header("Location", "/loop")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                    elif u.path == "/redir-nolocation":
                        self.send_response(302)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                    elif u.path == "/big":
                        n = int(q["n"][0])
                        self.send_response(200)
                        if q.get("length", ["1"])[0] == "1":
                            self.send_header("Content-Length", str(n))
                        self.end_headers()
                        if self.command != "HEAD":
                            self.wfile.write(b"x" * n)
                    elif u.path == "/gzip":
                        body = gzip.compress(b"hello")
                        self.send_response(200)
                        self.send_header("Content-Encoding", "gzip")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    elif u.path == "/identity":
                        self.send_response(200)
                        self.send_header("Content-Encoding", "identity")
                        self.send_header("Content-Length", "2")
                        self.end_headers()
                        self.wfile.write(b"ok")
                    elif u.path == "/slow":
                        self.send_response(200)
                        self.end_headers()
                        for _ in range(40):
                            self.wfile.write(b"x")
                            self.wfile.flush()
                            time.sleep(0.2)
                    elif u.path == "/steady":
                        self.send_response(200)
                        self.end_headers()
                        for _ in range(600):
                            self.wfile.write(b"x" * 1000)
                            self.wfile.flush()
                            time.sleep(0.005)
                    elif u.path == "/keepalive":
                        self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: keep-alive\r\n\r\nok")
                        self.wfile.flush()
                        time.sleep(2)                                 # a rude server: it keeps the connection open
                    elif u.path == "/bigheader":
                        self.send_response(200)
                        for i in range(30):
                            self.send_header("X-Pad-%d" % i, "y" * 900)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                    else:
                        body = b"hello"
                        self.send_response(200)
                        self.send_header("Content-Length", str(len(body)))
                        self.send_header("X-Seen", "1")
                        self.end_headers()
                        if self.command != "HEAD":
                            self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        class Quick(ThreadingHTTPServer):
            daemon_threads = True
            block_on_close = False    # do not wait for the slow-trickle handler when a test ends

        self.httpd = Quick(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def srv():
    s = Server()
    yield s
    s.close()


class Rig:
    """A SafeFetcher wired to the local server, with every resolver and connector call recorded."""

    def __init__(self, srv, answers=None, hosts=("api.example.com", "*.example.net"), **policy_kw):
        self.srv, self.lookups, self.connects = srv, [], []
        self.answers = answers or {}
        self.policy = FetchPolicy(allow_hosts=hosts, allow_ports=(srv.port,), allow_http=True, **policy_kw)
        self.kw = {}

    def resolver(self, host, port):
        self.lookups.append((host, port))
        a = self.answers.get(host, [PUBLIC])
        return a(len(self.lookups)) if callable(a) else a

    def connector(self, ip, port, timeout):
        self.connects.append(ip)
        return socket.create_connection(("127.0.0.1", self.srv.port), timeout=timeout)

    def fetcher(self, policy_for=None, **kw):
        return SafeFetcher(policy_for or (lambda scope: self.policy), _resolver=self.resolver, _connector=self.connector, **kw)

    def url(self, path, host="api.example.com"):
        return "http://%s:%d%s" % (host, self.srv.port, path)


# ---------------------------------------------------------------- the address rule

@pytest.mark.parametrize("addr,expected", [
    ("8.8.8.8", True), ("93.184.216.34", True), ("2606:4700:4700::1111", True), ("::ffff:8.8.8.8", True),
    ("64:ff9b::808:808", True), ("2002:0808:0808::", True),
    ("127.0.0.1", False), ("127.255.255.254", False), ("10.0.0.1", False), ("172.16.0.1", False), ("172.31.255.255", False),
    ("192.168.1.1", False), ("169.254.169.254", False), ("100.64.0.1", False), ("0.0.0.0", False), ("255.255.255.255", False),
    ("192.0.0.1", False), ("192.0.2.1", False), ("198.18.0.1", False), ("198.51.100.1", False), ("203.0.113.1", False),
    ("224.0.0.1", False), ("239.255.255.255", False), ("240.0.0.1", False),
    ("::1", False), ("::", False), ("fe80::1", False), ("fc00::1", False), ("fd00:ec2::254", False), ("ff02::1", False),
    ("::ffff:127.0.0.1", False), ("::ffff:169.254.169.254", False), ("::ffff:10.0.0.1", False),
    ("64:ff9b::7f00:1", False), ("64:ff9b::a9fe:a9fe", False), ("2002:7f00:1::", False), ("2002:a9fe:a9fe::", False),
    ("::127.0.0.1", False), ("::8.8.8.8", False), ("2001::1", False), ("2001:db8::1", False), ("64:ff9b:1::1", False),
    ("fe80::1%eth0", False), ("2606:4700:4700::1111%eth0", False), ("8.8.8.8%1", False), ("", False), ("1.2.3", False), ("not-an-ip", False), (" 8.8.8.8", False),
])
def test_the_address_rule(addr, expected):
    assert address_allowed(addr) is expected


@pytest.mark.parametrize("bad", [None, 5, b"8.8.8.8", ["8.8.8.8"]])
def test_the_address_rule_refuses_non_strings(bad):
    assert address_allowed(bad) is False


# ---------------------------------------------------------------- URL parsing and the allowlist

POLICY = FetchPolicy(allow_hosts=("api.example.com", "*.docs.example.org"), allow_ports=(443, 8443), allow_http=False)
HTTP_POLICY = FetchPolicy(allow_hosts=("api.example.com",), allow_ports=(80, 443), allow_http=True)


@pytest.mark.parametrize("url,expect", [
    ("https://api.example.com/x?y=1", ("https", "api.example.com", 443, "/x?y=1")),
    ("HTTPS://API.Example.COM", ("https", "api.example.com", 443, "/")),
    ("https://api.example.com?q=1", ("https", "api.example.com", 443, "/?q=1")),
    ("https://api.example.com:8443/a", ("https", "api.example.com", 8443, "/a")),
    ("https://a.docs.example.org/", ("https", "a.docs.example.org", 443, "/")),
    ("https://a.b.docs.example.org/", ("https", "a.b.docs.example.org", 443, "/")),
])
def test_good_urls_are_parsed_strictly(url, expect):
    t = _parse(url, POLICY)
    assert (t.scheme, t.host, t.port, t.target) == expect


@pytest.mark.parametrize("url,reason", [
    (None, "bad_url"), (5, "bad_url"), ("", "bad_url"), ("x" * 2049, "bad_url"), ("api.example.com/x", "bad_url"),
    ("https:/api.example.com", "bad_url"), ("https://api.example.com/a b", "bad_url"), ("https://api.example.com/a\tb", "bad_url"),
    ("https://api.example.com/a\nb", "bad_url"), ("https://api.example.com/\x00", "bad_url"),
    ("https://api.example.com/\u00e9", "bad_url"), ("https://api.example.com\\@evil.test", "bad_url"),
    ("https://api.example.com/x#frag", "bad_url"), ("https://api.example.com:/x", "bad_url"), ("https://api.example.com:0/x", "bad_url"),
    ("https://api.example.com:65536/x", "bad_url"), ("https://api.example.com:80a/x", "bad_url"),
    ("https://api.example.com:443:443/x", "bad_url"), ("https://api.example.com:-1/x", "bad_url"),
    ("ftp://api.example.com/x", "scheme_not_allowed"), ("file:///etc/passwd", "scheme_not_allowed"),
    ("gopher://api.example.com/", "scheme_not_allowed"), ("http://api.example.com/x", "scheme_not_allowed"),
    ("javascript://api.example.com/", "scheme_not_allowed"),
    ("https://user@api.example.com/", "userinfo"), ("https://user:pw@api.example.com/", "userinfo"),
    ("https://api.example.com@evil.test/", "userinfo"), ("https://evil.test@api.example.com/", "userinfo"),
    ("https://[::1]/", "ip_literal"), ("https://[2606:4700::1111]/", "ip_literal"),
    ("https://127.0.0.1/", "ip_literal"), ("https://8.8.8.8/", "ip_literal"), ("https://2130706433/", "ip_literal"),
    ("https://0x7f.1/", "ip_literal"), ("https://017700000001/", "ip_literal"), ("https://127.1/", "ip_literal"),
    ("https://0x7f000001/", "ip_literal"), ("https://10.1/", "ip_literal"),
    ("https:///x", "bad_host"), ("https://api.example.com./x", "bad_host"), ("https://-api.example.com/", "bad_host"),
    ("https://api..example.com/", "bad_host"), ("https://api_example.com/", "bad_host"), ("https://api.example.com%2f.evil.test/", "bad_host"),
    ("https://" + "a" * 64 + ".example.com/", "bad_host"), ("https://a b/", "bad_url"),
    ("https://evil.test/", "host_not_allowed"), ("https://example.com/", "host_not_allowed"),
    ("https://api.example.com.evil.test/", "host_not_allowed"), ("https://evil-api.example.com/", "host_not_allowed"),
    ("https://docs.example.org/", "host_not_allowed"), ("https://xdocs.example.org/", "host_not_allowed"),
    ("https://a.docs.example.org.evil.test/", "host_not_allowed"), ("https://localhost/", "host_not_allowed"),
    ("https://api.example.com:444/x", "port_not_allowed"), ("https://api.example.com:80/x", "port_not_allowed"),
])
def test_bad_urls_are_refused_with_a_fixed_reason(url, reason):
    with pytest.raises(NetError) as e:
        _parse(url, POLICY)
    assert e.value.reason == reason and str(e.value) == reason and reason in REASONS


def test_http_needs_the_policy_switch_and_then_uses_port_80():
    assert _parse("http://api.example.com/x", HTTP_POLICY).port == 80
    with pytest.raises(NetError, match="scheme_not_allowed"):
        _parse("http://api.example.com/x", POLICY)


@pytest.mark.parametrize("host,expect", [
    ("a.example.org", True), ("a.b.example.org", True), ("example.org", False), ("evil-example.org", False),
    ("example.org.evil.test", False), ("xexample.org", False), (".example.org", False),
])
def test_wildcards_match_whole_labels_only(host, expect):
    assert _host_allowed(host, ("*.example.org",)) is expect


def test_exact_names_match_exactly():
    assert _host_allowed("api.example.com", ("api.example.com",)) and not _host_allowed("x.api.example.com", ("api.example.com",))


@pytest.mark.parametrize("kw", [
    {"allow_hosts": ()}, {"allow_hosts": "api.example.com"}, {"allow_hosts": ("localhost",)}, {"allow_hosts": ("*.com",)},
    {"allow_hosts": ("*",)}, {"allow_hosts": ("*.*.example.com",)}, {"allow_hosts": ("a b.example.com",)}, {"allow_hosts": (5,)},
    {"allow_hosts": ("a.example.com", "")}, {"allow_hosts": ("127.0.0.1",)},
    {"allow_ports": ()}, {"allow_ports": (0,)}, {"allow_ports": (70000,)}, {"allow_ports": (True,)}, {"allow_ports": 443},
    {"allow_http": "yes"}, {"max_bytes": 0}, {"max_bytes": 16 * 1024 * 1024 + 1}, {"max_bytes": True}, {"max_bytes": 1.5},
    {"max_redirects": -1}, {"max_redirects": 6}, {"max_header_bytes": 10}, {"timeout": 0}, {"timeout": 121}, {"timeout": True},
    {"timeout": "5"},
])
def test_bad_policies_are_refused_when_built(kw):
    base = {"allow_hosts": ("api.example.com",)}
    with pytest.raises(ValueError):
        FetchPolicy(**{**base, **kw})
    FetchPolicy(**base)    # positive control


def test_policy_hosts_are_lowercased_and_frozen():
    p = FetchPolicy(allow_hosts=["API.Example.com"])
    assert p.allow_hosts == ("api.example.com",)
    with pytest.raises(Exception):
        p.timeout = 99


# ---------------------------------------------------------------- fetching

def test_a_get_returns_status_headers_and_body(srv):
    rig = Rig(srv)
    r = rig.fetcher().get(A, rig.url("/ok"))
    assert isinstance(r, FetchResult) and (r.status, r.body, r.host, r.redirects) == (200, b"hello", "api.example.com", 0)
    assert r.headers["x-seen"] == "1" and rig.lookups == [("api.example.com", srv.port)] and rig.connects == [PUBLIC]
    method, path, headers = srv.requests[0]
    assert headers["host"] == "api.example.com:%d" % srv.port and headers["accept-encoding"] == "identity"
    assert headers["user-agent"].startswith("ragleap-security-net/") and headers["connection"] == "close"


def test_a_head_returns_no_body(srv):
    r = Rig(srv).fetcher().head(A, "http://api.example.com:%d/big?n=5000" % srv.port)
    assert r.status == 200 and r.body == b"" and srv.requests[0][0] == "HEAD"


@pytest.mark.parametrize("blocked", ["127.0.0.1", "10.0.0.5", "169.254.169.254", "::1", "fd00::1", "100.64.0.9", "224.0.0.1",
                                     "::ffff:127.0.0.1", "64:ff9b::7f00:1", "::127.0.0.1"])
def test_a_name_that_resolves_to_a_blocked_address_is_refused_before_any_connection(srv, blocked):
    rig = Rig(srv, answers={"api.example.com": [blocked]})
    with pytest.raises(NetError) as e:
        rig.fetcher().get(A, rig.url("/ok"))
    assert e.value.reason == "address_blocked" and rig.connects == [] and srv.requests == []


def test_a_mixed_answer_is_refused_even_if_the_first_address_is_public(srv):
    rig = Rig(srv, answers={"api.example.com": [PUBLIC, "127.0.0.1"]})
    with pytest.raises(NetError, match="address_blocked"):
        rig.fetcher().get(A, rig.url("/ok"))
    assert rig.connects == []


def test_dns_rebinding_does_not_work_one_lookup_and_the_connection_uses_the_checked_address(srv):
    rig = Rig(srv, answers={"api.example.com": lambda n: [PUBLIC] if n == 1 else ["127.0.0.1"]})
    r = rig.fetcher().get(A, rig.url("/ok"))
    assert r.status == 200 and len(rig.lookups) == 1 and rig.connects == [PUBLIC]


@pytest.mark.parametrize("answer", [OSError("no such host"), socket.gaierror(-2, "x"), UnicodeError("bad"), []])
def test_dns_failures(srv, answer):
    def resolver(host, port):
        if isinstance(answer, BaseException):
            raise answer
        return answer
    f = SafeFetcher(lambda s: Rig(srv).policy, _resolver=resolver, _connector=lambda *a: pytest.fail("connected"))
    with pytest.raises(NetError, match="dns_failed"):
        f.get(A, "http://api.example.com:%d/" % srv.port)


def test_too_many_addresses_are_refused(srv):
    rig = Rig(srv, answers={"api.example.com": ["93.184.216.%d" % i for i in range(1, 20)]})
    with pytest.raises(NetError, match="dns_failed"):
        rig.fetcher().get(A, rig.url("/ok"))


def test_a_failed_first_address_falls_back_to_the_next_one_up_to_four_tries(srv):
    tried = []

    def connector(ip, port, timeout):
        tried.append(ip)
        if ip != "93.184.216.4":
            raise ConnectionRefusedError()
        return socket.create_connection(("127.0.0.1", srv.port), timeout=timeout)

    answers = ["93.184.216.%d" % i for i in range(1, 8)]
    pol = Rig(srv).policy
    f = SafeFetcher(lambda s: pol, _resolver=lambda h, p: answers, _connector=connector)
    assert f.get(A, "http://api.example.com:%d/ok" % srv.port).status == 200 and tried == answers[:4]
    tried.clear()
    f2 = SafeFetcher(lambda s: pol, _resolver=lambda h, p: answers[4:], _connector=connector)
    with pytest.raises(NetError, match="connect_failed"):
        f2.get(A, "http://api.example.com:%d/ok" % srv.port)
    assert len(tried) == 3


def test_connect_timeout_gets_its_own_reason(srv):
    def connector(ip, port, timeout):
        raise TimeoutError()
    f = SafeFetcher(lambda s: Rig(srv).policy, _resolver=lambda h, p: [PUBLIC], _connector=connector)
    with pytest.raises(NetError, match="timeout"):
        f.get(A, "http://api.example.com:%d/" % srv.port)


def test_https_to_a_server_that_does_not_speak_tls_fails_as_tls_failed(srv):
    rig = Rig(srv)
    pol = FetchPolicy(allow_hosts=("api.example.com",), allow_ports=(srv.port,))
    f = SafeFetcher(lambda s: pol, _resolver=rig.resolver, _connector=rig.connector)
    with pytest.raises(NetError) as e:
        f.get(A, "https://api.example.com:%d/ok" % srv.port)
    assert e.value.reason == "tls_failed" and rig.connects == [PUBLIC]


def test_the_tls_context_verifies_certificates_and_names_and_needs_tls_1_2():
    ctx = SafeFetcher(lambda s: None)._ctx
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True
    assert ctx.minimum_version >= ssl.TLSVersion.TLSv1_2


# ---------------------------------------------------------------- redirects

def test_a_redirect_is_followed_and_every_hop_is_checked_again(srv):
    rig = Rig(srv)
    r = rig.fetcher().get(A, rig.url("/redir?to=" + rig.url("/ok", "docs.example.net")))
    assert r.status == 200 and r.redirects == 1 and r.host == "docs.example.net"
    assert [h for h, _ in rig.lookups] == ["api.example.com", "docs.example.net"]            # a lookup and a check per hop


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_all_five_redirect_codes_are_followed(srv, code):
    rig = Rig(srv)
    assert rig.fetcher().get(A, rig.url("/redir?code=%d&to=/ok" % code)).redirects == 1


def test_a_relative_redirect_stays_on_the_same_host(srv):
    rig = Rig(srv)
    r = rig.fetcher().get(A, rig.url("/redir?to=/ok"))
    assert r.status == 200 and r.host == "api.example.com" and r.redirects == 1


@pytest.mark.parametrize("to,reason", [
    ("http://evil.test:%(p)d/ok", "host_not_allowed"), ("http://127.0.0.1:%(p)d/ok", "ip_literal"),
    ("http://[::1]:%(p)d/ok", "ip_literal"), ("http://2130706433:%(p)d/ok", "ip_literal"),
    ("http://api.example.com:1/ok", "port_not_allowed"), ("//evil.test/ok", "host_not_allowed"),
    ("https://api.example.com:%(p)d/ok", "tls_failed"),
    ("ftp://api.example.com/ok", "scheme_not_allowed"), ("file:///etc/passwd", "scheme_not_allowed"),
    ("http://user@api.example.com:%(p)d/ok", "userinfo"),
])
def test_a_redirect_to_anything_outside_the_policy_is_refused(srv, to, reason):
    rig = Rig(srv)
    with pytest.raises(NetError) as e:
        rig.fetcher().get(A, rig.url("/redir?to=" + to.replace("%(p)d", str(srv.port))))
    assert e.value.reason == reason


@pytest.mark.parametrize("to", ["/ok\\x", "/a\x01b", "/a b", "/\u00e9", ""])
def test_a_malformed_location_is_refused(to):
    # a Location the test server cannot send in clear text is checked through the helper the fetcher uses
    with pytest.raises(NetError, match="bad_redirect"):
        SafeFetcher._next_url("http://api.example.com/", to, "api.example.com")


def test_a_redirect_to_a_host_that_resolves_to_a_private_address_is_refused(srv):
    rig = Rig(srv, answers={"docs.example.net": ["10.0.0.7"]})
    with pytest.raises(NetError, match="address_blocked"):
        rig.fetcher().get(A, rig.url("/redir?to=" + rig.url("/ok", "docs.example.net")))
    assert rig.connects == [PUBLIC]                                                       # only the first hop ever connected


def test_the_redirect_cap_and_loops(srv):
    rig = Rig(srv, max_redirects=2)
    with pytest.raises(NetError, match="too_many_redirects"):
        rig.fetcher().get(A, rig.url("/loop"))
    assert len(rig.lookups) == 3                                                          # the original request and two redirects
    rig0 = Rig(srv, max_redirects=0)
    with pytest.raises(NetError, match="too_many_redirects"):
        rig0.fetcher().get(A, rig0.url("/redir?to=/ok"))
    rig1 = Rig(srv, max_redirects=1)
    assert rig1.fetcher().get(A, rig1.url("/redir?to=/ok")).redirects == 1


def test_a_redirect_status_without_a_location_is_returned_as_it_is(srv):
    r = Rig(srv).fetcher().get(A, Rig(srv).url("/redir-nolocation"))
    assert r.status == 302 and r.redirects == 0


def test_caller_headers_go_to_the_first_origin_only_and_never_again_after_leaving_it(srv):
    rig = Rig(srv)
    other = rig.url("/redir?to=" + rig.url("/ok"), "docs.example.net")                    # B -> A again
    start = rig.url("/redir?to=" + other)                                                 # A -> B -> A
    r = rig.fetcher().get(A, start, headers={"X-Token": "secret", "Authorization": "Bearer abc"})
    assert r.status == 200 and r.redirects == 2
    seen = [h for _, _, h in srv.requests]
    assert "x-token" in seen[0] and "authorization" in seen[0]
    assert all("x-token" not in h and "authorization" not in h for h in seen[1:])
    srv.requests.clear()
    same = rig.fetcher().get(A, rig.url("/redir?to=/ok"), headers={"X-Token": "secret"})
    assert all("x-token" in h for _, _, h in srv.requests) and same.status == 200


# ---------------------------------------------------------------- caller headers

@pytest.mark.parametrize("headers", [
    {"Host": "evil.test"}, {"host": "x"}, {"Content-Length": "5"}, {"Transfer-Encoding": "chunked"}, {"Connection": "keep-alive"},
    {"Accept-Encoding": "gzip"}, {"Upgrade": "websocket"}, {"TE": "trailers"}, {"Expect": "100-continue"},
    {"Proxy-Authorization": "x"}, {"X-A": "a\r\nX-B: b"}, {"X-A": "a\nb"}, {"X-A": "caf\u00e9"}, {"X A": "b"}, {"1X": "b"},
    {"X-A": "v" * 1025}, {"": "x"}, {"X-A": 5}, {5: "x"}, ["X-A", "b"], "X-A: b", {"X-%d" % i: "v" for i in range(21)},
])
def test_bad_caller_headers_are_refused_before_any_lookup(srv, headers):
    rig = Rig(srv)
    with pytest.raises(NetError, match="bad_header"):
        rig.fetcher().get(A, rig.url("/ok"), headers=headers)
    assert rig.lookups == []


def test_good_caller_headers_are_sent(srv):
    rig = Rig(srv)
    rig.fetcher().get(A, rig.url("/ok"), headers={"X-Request-Id": "abc 123", "Accept": "application/json"})
    h = srv.requests[0][2]
    assert h["x-request-id"] == "abc 123" and h["accept"] == "application/json"


# ---------------------------------------------------------------- response limits

def test_a_declared_length_over_the_cap_is_refused_before_reading(srv):
    rig = Rig(srv, max_bytes=1000)
    with pytest.raises(NetError, match="response_too_large"):
        rig.fetcher().get(A, rig.url("/big?n=5000"))


def test_a_body_without_a_length_is_cut_off_at_the_cap_and_refused_not_truncated(srv):
    rig = Rig(srv, max_bytes=1000)
    with pytest.raises(NetError, match="response_too_large"):
        rig.fetcher().get(A, rig.url("/big?n=200000&length=0"))


def test_a_body_exactly_at_the_cap_is_fine(srv):
    rig = Rig(srv, max_bytes=1000)
    assert len(rig.fetcher().get(A, rig.url("/big?n=1000")).body) == 1000
    assert len(rig.fetcher().get(A, rig.url("/big?n=1000&length=0")).body) == 1000
    with pytest.raises(NetError, match="response_too_large"):
        rig.fetcher().get(A, rig.url("/big?n=1001&length=0"))


def test_a_slow_trickle_cannot_outlast_the_deadline(srv):
    rig = Rig(srv, timeout=0.8)
    t0 = time.monotonic()
    with pytest.raises(NetError, match="timeout"):
        rig.fetcher().get(A, rig.url("/slow"))
    assert time.monotonic() - t0 < 3


def test_content_encoding_other_than_identity_is_refused(srv):
    rig = Rig(srv)
    with pytest.raises(NetError, match="content_encoding"):
        rig.fetcher().get(A, rig.url("/gzip"))
    assert rig.fetcher().get(A, rig.url("/identity")).body == b"ok"


def test_huge_response_headers_are_refused(srv):
    rig = Rig(srv, max_header_bytes=4096)
    with pytest.raises(NetError, match="bad_response"):
        rig.fetcher().get(A, rig.url("/bigheader"))


def test_a_connection_that_closes_without_a_response_is_a_bad_response(srv):
    a, b = socket.socketpair()
    b.close()
    pol = Rig(srv).policy
    f = SafeFetcher(lambda s: pol, _resolver=lambda h, p: [PUBLIC], _connector=lambda ip, port, t: a)
    with pytest.raises(NetError, match="bad_response"):
        f.get(A, "http://api.example.com:%d/" % srv.port)


def test_environment_proxies_are_ignored(srv, monkeypatch):
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
        monkeypatch.setenv(k, "http://127.0.0.1:9")
    rig = Rig(srv)
    assert rig.fetcher().get(A, rig.url("/ok")).body == b"hello" and rig.connects == [PUBLIC]


# ---------------------------------------------------------------- tenants, rate limit, audit

def test_the_policy_decides_per_tenant_and_a_missing_or_bad_policy_refuses(srv):
    rig = Rig(srv)
    acme = rig.policy
    globex = FetchPolicy(allow_hosts=("other.example.com",), allow_ports=(srv.port,), allow_http=True)
    f = rig.fetcher(lambda s: acme if s == A else globex)
    assert f.get(A, rig.url("/ok")).status == 200
    with pytest.raises(NetError, match="host_not_allowed"):
        f.get(B, rig.url("/ok"))
    assert f.get(B, rig.url("/ok", "other.example.com")).status == 200
    for bad, reason in ((None, "no_policy"), ("x", "bad_policy"), ({"allow_hosts": ["api.example.com"]}, "bad_policy"), (5, "bad_policy")):
        with pytest.raises(NetError) as e:
            rig.fetcher(lambda s, b=bad: b).get(A, rig.url("/ok"))
        assert e.value.reason == reason


def test_a_policy_subclass_is_not_trusted(srv):
    class Loose(FetchPolicy):
        pass
    rig = Rig(srv)
    with pytest.raises(NetError, match="bad_policy"):
        rig.fetcher(lambda s: Loose(allow_hosts=("api.example.com",), allow_ports=(srv.port,), allow_http=True)).get(A, rig.url("/ok"))


def test_an_exception_in_policy_for_is_hidden(srv):
    def boom(scope):
        raise RuntimeError("secret detail abc")
    rig = Rig(srv)
    with pytest.raises(NetError) as e:
        rig.fetcher(boom).get(A, rig.url("/ok"))
    assert e.value.reason == "bad_policy" and "secret" not in str(e.value) and e.value.__cause__ is None


@pytest.mark.parametrize("bad", [None, "acme", {"tenant": "acme"}, 5])
def test_only_a_real_scope_is_accepted(srv, bad):
    rig = Rig(srv)
    with pytest.raises(ScopeError):
        rig.fetcher().get(bad, rig.url("/ok"))
    assert rig.lookups == []


def test_the_rate_limiter_is_checked_before_any_lookup_and_per_scope(srv):
    rig = Rig(srv)
    f = rig.fetcher(rate_limiter=RateLimiter(1.0, 1, clock=lambda: 1000.0))
    assert f.get(A, rig.url("/ok")).status == 200
    n = len(rig.lookups)
    with pytest.raises(RateLimitError):
        f.get(A, rig.url("/ok"))
    assert len(rig.lookups) == n and f.get(B, rig.url("/ok")).status == 200


def test_the_audit_log_holds_hosts_and_numbers_never_paths_queries_headers_or_bodies(srv, tmp_path):
    log = AuditLog(tmp_path / "a.jsonl")
    rig = Rig(srv)
    f = rig.fetcher(audit=log)
    f.get(A, rig.url("/redir?to=" + rig.url("/ok?token=abc123", "docs.example.net")), headers={"X-Token": "hunter2"})
    with pytest.raises(NetError):
        f.get(B, "http://evil.test:%d/steal?x=1" % srv.port)
    raw = open(log.path).read()
    for secret in ("abc123", "hunter2", "/redir", "/ok", "steal", "x=1", "hello", "token"):
        assert secret not in raw
    events = [json.loads(l) for l in raw.splitlines()]
    assert [e["event"] for e in events] == ["net.redirect", "net.request", "net.blocked"]
    assert events[0]["data"] == {"host": "docs.example.net", "port": srv.port, "hops": 1}
    assert events[1]["data"] == {"host": "docs.example.net", "port": srv.port, "status": 200, "bytes": 5, "hops": 1}
    assert events[2]["data"] == {"reason": "host_not_allowed", "host": "evil.test", "hops": 0} and events[2]["scope"] == "tenant=globex"
    assert log.verify().ok


def test_a_failed_audit_write_raises_and_the_result_is_not_returned(srv):
    class Broken(AuditLog):
        def __init__(self):
            pass

        def append(self, *a, **k):
            raise OSError("disk full")

    rig = Rig(srv)
    with pytest.raises(OSError):
        rig.fetcher(audit=Broken()).get(A, rig.url("/ok"))


def test_blocked_events_name_the_reason_even_when_the_host_is_unknown(srv, tmp_path):
    log = AuditLog(tmp_path / "a.jsonl")
    rig = Rig(srv)
    with pytest.raises(NetError):
        rig.fetcher(audit=log).get(A, "ftp://x/")
    ev = json.loads(open(log.path).readline())
    assert ev["event"] == "net.blocked" and ev["data"] == {"reason": "scheme_not_allowed", "host": "", "hops": 0}


def test_every_error_reason_is_a_known_code(srv):
    rig = Rig(srv)
    for url in ("ftp://x/", "http://evil.test/", "https://127.0.0.1/"):
        with pytest.raises(NetError) as e:
            rig.fetcher().get(A, url)
        assert e.value.reason in REASONS and url not in str(e.value) and e.value.args == (e.value.reason,)


def test_the_fetcher_checks_its_configuration():
    for kw in ({"audit": "x"}, {"rate_limiter": object()}, {"clock": 5}, {"_resolver": 5}, {"_connector": "x"}, {"_address_rule": 1}):
        with pytest.raises(ValueError):
            SafeFetcher(lambda s: None, **kw)
    with pytest.raises(ValueError):
        SafeFetcher("not callable")
    SafeFetcher(lambda s: None)      # positive control


class Tracked:
    """Wraps a socket and counts close() calls, so a test can see that nothing is left open."""
    def __init__(self, sock, log):
        self._sock, self._log = sock, log

    def __getattr__(self, name):
        return getattr(self._sock, name)

    def close(self):
        self._log.append("close")
        self._sock.close()


@pytest.mark.parametrize("path,error", [("/ok", None), ("/big?n=5000", "response_too_large"), ("/gzip", "content_encoding"),
                                        ("/redir?to=http://evil.test/", "host_not_allowed")])
def test_the_socket_is_closed_after_every_request_also_on_errors(srv, path, error):
    closes = []
    rig = Rig(srv, max_bytes=1000)

    def connector(ip, port, timeout):
        return Tracked(socket.create_connection(("127.0.0.1", srv.port), timeout=timeout), closes)

    f = SafeFetcher(lambda s: rig.policy, _resolver=rig.resolver, _connector=connector)
    if error:
        with pytest.raises(NetError, match=error):
            f.get(A, rig.url(path))
    else:
        assert f.get(A, rig.url(path)).status == 200
    assert len(closes) >= 1


def test_the_real_resolver_and_address_rule_refuse_localhost_and_an_unknown_name_fails_cleanly(srv):
    from ragleap_security.net import _default_resolver

    addrs = _default_resolver("localhost", 80)
    assert addrs and not any(address_allowed(a) for a in addrs)
    pol = FetchPolicy(allow_hosts=("no-such-host.invalid",), allow_ports=(srv.port,), allow_http=True)
    with pytest.raises(NetError, match="dns_failed"):
        SafeFetcher(lambda s: pol).get(A, "http://no-such-host.invalid:%d/" % srv.port)


def test_the_real_connector_connects_and_reports_refusal(srv):
    from ragleap_security.net import _default_connector

    sock = _default_connector("127.0.0.1", srv.port, 2.0)
    sock.close()
    with pytest.raises(OSError):
        _default_connector("127.0.0.1", 1, 1.0)


def test_an_embedded_ipv4_is_judged_as_that_ipv4_whatever_the_python_version(monkeypatch):
    import ipaddress

    import ragleap_security.net as net
    seen = []
    monkeypatch.setattr(net, "_v4_ok", lambda ip: seen.append(str(ip)) or False)
    for addr, v4 in (("::ffff:8.8.8.8", "8.8.8.8"), ("64:ff9b::808:808", "8.8.8.8"), ("2002:0808:0808::", "8.8.8.8")):
        seen.clear()
        assert net.address_allowed(addr) is False and seen == [v4], addr
    assert isinstance(ipaddress.ip_address("8.8.8.8"), ipaddress.IPv4Address)


def test_at_most_four_addresses_are_tried(srv):
    tried = []

    def connector(ip, port, timeout):
        tried.append(ip)
        raise ConnectionRefusedError()

    pol = Rig(srv).policy
    f = SafeFetcher(lambda s: pol, _resolver=lambda h, p: ["93.184.216.%d" % i for i in range(1, 9)], _connector=connector)
    with pytest.raises(NetError, match="connect_failed"):
        f.get(A, "http://api.example.com:%d/" % srv.port)
    assert len(tried) == 4


def test_a_steady_slow_stream_is_cut_off_by_the_deadline_not_read_to_the_end(srv):
    rig = Rig(srv, timeout=0.5, max_bytes=16 * 1024 * 1024)
    t0 = time.monotonic()
    with pytest.raises(NetError, match="timeout"):
        rig.fetcher().get(A, rig.url("/steady"))                 # the server would send 600 KB over about 3 seconds
    assert time.monotonic() - t0 < 2


def test_a_deadline_that_ran_out_during_one_hop_stops_before_the_next_lookup(srv):
    class Clock:
        t = 0.0

        def __call__(self):
            return self.t
    clock, lookups = Clock(), []

    def connector(ip, port, timeout):
        clock.t += 100                                           # the first connection was "slow"
        return socket.create_connection(("127.0.0.1", srv.port), timeout=5)

    pol = Rig(srv, timeout=10).policy
    f = SafeFetcher(lambda s: pol, clock=clock, _resolver=lambda h, p: lookups.append(h) or [PUBLIC], _connector=connector)
    with pytest.raises(NetError, match="timeout"):
        f.get(A, "http://api.example.com:%d/redir?to=/ok" % srv.port)
    assert lookups == ["api.example.com"]


def test_the_raw_socket_is_closed_when_the_tls_handshake_fails(srv):
    made = []

    def connector(ip, port, timeout):
        sock = socket.create_connection(("127.0.0.1", srv.port), timeout=timeout)
        made.append(sock)
        return sock

    pol = FetchPolicy(allow_hosts=("api.example.com",), allow_ports=(srv.port,))
    f = SafeFetcher(lambda s: pol, _resolver=lambda h, p: [PUBLIC], _connector=connector)
    with pytest.raises(NetError, match="tls_failed"):
        f.get(A, "https://api.example.com:%d/ok" % srv.port)
    assert len(made) == 1 and made[0].fileno() == -1


def test_the_deadline_is_checked_between_reads_even_when_the_data_is_already_waiting(srv):
    class StepClock:
        """Every look at the clock costs 0.1 s, so the deadline passes after a few reads of a body that is ready at once."""
        t = 0.0

        def __call__(self):
            self.t += 0.1
            return self.t

    rig = Rig(srv, timeout=1.0, max_bytes=16 * 1024 * 1024)
    ok = rig.fetcher(clock=lambda: 0.0).get(A, rig.url("/big?n=2000000"))        # positive control: a frozen clock never expires
    assert len(ok.body) == 2000000
    with pytest.raises(NetError, match="timeout"):
        rig.fetcher(clock=StepClock()).get(A, rig.url("/big?n=2000000"))


def test_our_socket_is_closed_even_if_the_server_ignores_connection_close(srv):
    made = []

    def connector(ip, port, timeout):
        sock = socket.create_connection(("127.0.0.1", srv.port), timeout=timeout)
        made.append(sock)
        return sock

    rig = Rig(srv)
    f = SafeFetcher(lambda s: rig.policy, _resolver=rig.resolver, _connector=connector)
    assert f.get(A, rig.url("/keepalive")).body == b"ok"
    assert len(made) == 1 and made[0].fileno() == -1
