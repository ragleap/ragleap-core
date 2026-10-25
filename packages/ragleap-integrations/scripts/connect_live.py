#!/usr/bin/env python3
"""Read-only live check of ragleap-integrations connect() against a real MCP server.
tools/list only: no credentials, no tools/call. Server text is untrusted and printed with repr().
Usage: python connect_live.py [url]   (default https://pubsub.googleapis.com/mcp)"""
import json, sys, urllib.request
import ragleap_integrations as ri
from ragleap_integrations import connect

URL = "https://pubsub.googleapis.com/mcp"
V = "2026-07-28"
HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")

def clip(x, n=160):
    return repr(x)[:n]

def fetch_raw(url):
    meta = {"io.modelcontextprotocol/protocolVersion": V, "io.modelcontextprotocol/clientCapabilities": {}}
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": meta}}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json",
        "MCP-Protocol-Version": V, "Mcp-Method": "tools/list"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return [t for t in json.loads(r.read(8000000))["result"]["tools"] if isinstance(t, dict)]

def expected_flag(raw_annotations, trust):
    a = raw_annotations if isinstance(raw_annotations, dict) else {}
    h = {k: a[k] for k in HINTS if isinstance(a.get(k), bool)}
    return not (trust and h.get("readOnlyHint") is True and h.get("destructiveHint") is not True), h

def main(url=URL, raw=None, transport=None):
    print("client:", ri.__version__, "| target:", url)
    tools = raw if raw is not None else fetch_raw(url)
    names = [t["name"] for t in tools if isinstance(t.get("name"), str)]
    print("RAW tools/list: %d tools" % len(tools))
    for t in tools:
        print("  %-22s annotations=%s" % (t.get("name"), clip(t.get("annotations"))))
    by_name = {t["name"]: t for t in tools if isinstance(t.get("name"), str)}
    bad = 0
    for trust in (False, True):
        events = []
        conn = connect(url, allow=names, name="g", trust_annotations=trust, on_event=events.append, transport=transport)
        print("\nconnect(trust_annotations=%s):" % trust)
        print(conn.report())
        print("events:", clip(events, 400))
        mism = []
        for t in conn.tools:
            m = t.metadata["mcp"]
            exp, hints = expected_flag(by_name[m["tool"]].get("annotations"), trust)
            if m["requires_approval"] != exp or m["annotations"] != hints:
                mism.append((m["tool"], m["requires_approval"], exp))
        relaxed = [t.metadata["mcp"]["tool"] for t in conn.tools if not t.metadata["mcp"]["requires_approval"]]
        print("flags vs independent computation from the raw hints: %s (%d tools, %d without approval: %s)"
              % ("MATCH" if not mism else "MISMATCH %s" % mism, len(conn.tools), len(relaxed), relaxed))
        bad += len(mism)
        if any(e.get("event") == "call" for e in events): print("UNEXPECTED call event"); bad += 1
    print("\nDONE:", "PASS" if bad == 0 else "FAIL", "(about the flag logic on this server's hints only; no tools/call was made)")
    return 0 if bad == 0 else 1

if __name__ == "__main__":
    sys.exit(main(*(sys.argv[1:2])))
