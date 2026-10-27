#!/usr/bin/env python3
"""Live two-tenant check of ragleap-integrations McpHub against a third party's public
mock MCP auth server (https://mcpplaygroundonline.com/mock-mcp-servers; read their page
first). Read-only: it mints tokens, lists tools and calls get_user. Tokens are held in memory
and never printed; only whether two tokens differ is shown. Server text is untrusted and is
printed with repr(). Usage: python tenant_live.py"""
import hashlib
import json
import sys
import time
import urllib.request

from ragleap_integrations import McpClient, McpConfig, McpDiscoveryError, McpHub, McpServerConfig, RateLimit, Scope

BASE = "https://mcpplaygroundonline.com"
UA = "ragleap-live-check (+https://github.com/ragleap/ragleap-core)"
REDACT = []


def show(check, verdict, detail=""):
    print("%-3s %-5s %s" % (check, verdict, detail), flush=True)


def clip(value, n=200):
    text = repr(value)
    for t in REDACT:
        text = text.replace(t, "[token]")
    return text[:n]


def mint_token():
    req = urllib.request.Request(BASE + "/api/auth/token", data=b"", method="POST", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        obj = json.loads(r.read(20000).decode("utf-8", "replace"))
    for k in ("access_token", "token"):
        if isinstance(obj, dict) and isinstance(obj.get(k), str) and obj[k]:
            return obj[k]
    return None


def h12(auth):
    return hashlib.sha256((auth or "").encode()).hexdigest()[:12]


class Rec:
    """Wraps the real transport; remembers a hash of each request's Authorization header."""

    def __init__(self, real):
        self.real = real
        self.rows = []
        self.tag = None

    def __call__(self, *a, **k):
        auth = a[1].get("Authorization")
        self.rows.append((self.tag, h12(auth), json.loads(a[2])["method"]))
        return self.real(*a, **k)


def act(rec, tag, fn, *args):
    """Run fn while every request it makes is tagged with the acting tenant."""
    rec.tag = tag
    try:
        return fn(*args)
    finally:
        rec.tag = None


def get_user(conn):
    return conn.toolkit.call("auth__get_user", "{}")


def main(mint=mint_token, real=None):
    url = BASE + "/mcp-auth-server"
    cfg = McpConfig(servers=[McpServerConfig("auth", url)], allowed_tools=["auth.get_user"])
    real = real or McpClient(cfg)._default_transport
    bad = 0
    tokens = {}
    for tenant in ("acme", "globex"):
        t = mint()
        if not t:
            show("T0", "SKIP", "no token field in the mint response")
            return 1
        tokens[tenant] = t
        REDACT.append(t)
        time.sleep(1)
    minted = dict(tokens)  # the real tokens, kept for the leak check after one is revoked below
    distinct = tokens["acme"] != tokens["globex"]
    show("T0", "INFO", "two tokens minted, not shown; the two are different: %s" % distinct)
    rec, events = Rec(real), []
    hub = McpHub(McpConfig(servers=[McpServerConfig("auth", url)], allowed_tools=["auth.get_user"], on_event=events.append),
                 token_provider=lambda scope, server: tokens.get(scope.tenant, "example-invalid"), transport=rec)
    acme = act(rec, "acme", hub.connect, Scope(tenant="acme", subject="u1"))
    globex = act(rec, "globex", hub.connect, Scope(tenant="globex", subject="u9"))
    ra, rb = act(rec, "acme", get_user, acme), act(rec, "globex", get_user, globex)
    ok = ra.success and rb.success
    show("T1", "PASS" if ok else "FAIL", "both tenants discovered and called get_user with their own token: %s / %s" % (ra.success, rb.success))
    bad += 0 if ok else 1
    # each request carried only its own tenant's token (compare hashes; the tokens stay private)
    if not distinct:
        show("T2", "SKIP", "the server issued the same token twice, so separation cannot be shown")
    else:
        ha, hb = h12("Bearer " + tokens["acme"]), h12("Bearer " + tokens["globex"])
        good = len(rec.rows) >= 4 and all(r[0] in ("acme", "globex") and r[1] == (ha if r[0] == "acme" else hb) for r in rec.rows)
        show("T2", "PASS" if good else "FAIL", "%d requests, each carried only its own tenant's token: %s" % (len(rec.rows), good))
        bad += 0 if good else 1
    # an unknown tenant with a token the server never issued is refused, and the others are unaffected
    try:
        hub.connect(Scope(tenant="mallory"))
        show("T3", "FAIL", "a made-up token was accepted")
        bad += 1
    except McpDiscoveryError as e:
        refused = "401" in str(e) or "403" in str(e)
        show("T3", "PASS" if refused else "INFO", "made-up token refused: %s" % clip(str(e)))
        bad += 0 if refused else 1
    still = get_user(acme).success and get_user(globex).success
    show("T3b", "PASS" if still else "FAIL", "the real tenants still work after the refusal: %s" % still)
    bad += 0 if still else 1
    # the token is resolved per request: revoke one tenant and only that tenant stops working
    tokens["globex"] = "example-revoked"
    r_rev, r_ok = get_user(globex), get_user(acme)
    revoked = (not r_rev.success) and ("401" in str(r_rev.error) or "403" in str(r_rev.error)) and r_ok.success
    show("T4", "PASS" if revoked else "FAIL", "changed globex's token: globex refused (%s), acme unaffected (%s)" % (clip(r_rev.error), r_ok.success))
    bad += 0 if revoked else 1
    # rate limit per tenant, enforced before the network
    rec2 = Rec(real)
    hub2 = McpHub(McpConfig(servers=[McpServerConfig("auth", url)], allowed_tools=["auth.get_user"]),
                  token_provider=lambda scope, server: tokens["acme"], limit=RateLimit(2, 60), transport=rec2)
    conn = hub2.connect(Scope(tenant="acme", subject="u1"))
    res = [get_user(conn) for _ in range(3)]
    sent = len([r for r in rec2.rows if r[2] == "tools/call"])
    lim = [r.success for r in res] == [True, True, False] and res[2].error == "MCP call limit reached" and sent == 2
    show("T5", "PASS" if lim else "FAIL", "RateLimit(2, 60): results %s, tools/call requests that reached the server: %d" % ([r.success for r in res], sent))
    bad += 0 if lim else 1
    # events carry the tenant and never a token
    dump = json.dumps(events, default=str)
    clean = all(t not in dump for t in minted.values()) and all("tenant" in e for e in events) and len(events) >= 4
    show("T6", "PASS" if clean else "FAIL", "%d events, all with a tenant, none with a token: %s" % (len(events), clean))
    bad += 0 if clean else 1
    print("\ndone: %s (about this server and this client only; the mock server cannot tell tenants apart, so separation is shown from the requests we sent)" % ("PASS" if bad == 0 else "FAIL"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
