#!/usr/bin/env python3
"""Read-only live check of the UTCP client against a real manual URL.

    python scripts/utcp_live.py https://api.example.com/utcp
    python scripts/utcp_live.py URL --allow get_order,list_orders --origins https://data.example.net
    python scripts/utcp_live.py URL --allowed-query format
    python scripts/utcp_live.py URL --allow get_order --call get_order --args '{"id": "1"}'

Without --call it only reads the manual (two GETs: a raw one, then one inside connect_utcp) and prints what connect_utcp() would offer;
nothing else is requested. With --call it makes exactly one tool call, which you chose, so only
use it on a tool that is safe to call. The check is repository-only (not in the wheel). Everything
a server sends is printed with repr(): it is untrusted text. No token is read from the environment.
"""
import argparse
import json
import sys

from ragleap_integrations import McpConfigError, McpDiscoveryError, _net, connect_utcp


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("url")
    ap.add_argument("--allow", help="comma-separated tool names (default: every http tool in the manual, at most 20)")
    ap.add_argument("--origins", default="", help="comma-separated extra https origins the tools may call")
    ap.add_argument("--allowed-query", default="", help="comma-separated static query parameter names the URLs may carry")
    ap.add_argument("--call", help="call this one tool after discovery")
    ap.add_argument("--args", default="{}", help="JSON arguments for --call")
    a = ap.parse_args(argv)

    try:
        raw = _net.https_request("GET", a.url, {"Accept": "application/json"}, None)
    except _net.TransportError as e:
        print("manual request failed:", e.message); return 1
    print("manual: HTTP %d, %d bytes, content-type %r" % (raw.status, len(raw.body), raw.content_type))
    try:
        doc = json.loads(raw.body.decode("utf-8"))
    except ValueError:
        print("the reply is not JSON"); return 1
    tools = doc.get("tools") if isinstance(doc, dict) else None
    print("utcp_version: %r, tools: %s" % (doc.get("utcp_version") if isinstance(doc, dict) else None,
                                            len(tools) if isinstance(tools, list) else "n/a"))
    kinds = {}
    for t in tools if isinstance(tools, list) else []:
        tpl = t.get("tool_call_template") if isinstance(t, dict) else None
        k = tpl.get("call_template_type") if isinstance(tpl, dict) else None
        kinds[repr(k)] = kinds.get(repr(k), 0) + 1
    print("call template types:", kinds)

    if a.allow:
        allow = [x for x in a.allow.split(",") if x]
    else:
        allow = [t["name"] for t in (tools or []) if isinstance(t, dict) and isinstance(t.get("name"), str)
                 and isinstance(t.get("tool_call_template"), dict)
                 and t["tool_call_template"].get("call_template_type") == "http"][:20]
    if not allow:
        print("no allowlist to try"); return 1
    origins = [x for x in a.origins.split(",") if x]
    try:
        conn = connect_utcp(a.url, allow=allow, origins=origins, allowed_query=[x for x in a.allowed_query.split(",") if x])
    except (McpConfigError, McpDiscoveryError) as e:
        print("connect_utcp refused:", e); return 1
    print(conn.report())
    if a.call:
        name = "utcp__" + a.call
        res = conn.toolkit.call(name, a.args)
        print("call %s -> success=%s error=%r result=%r" % (name, res.success, res.error, str(res.result)[:300]))
        return 0 if res.success else 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
