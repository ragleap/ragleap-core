"""Child side of the isolated-worker protocol (docs/design/api.md section 8). Standard library only.

A worker script loads this file by path (importlib), because the worker environment may not have ragleap_voice installed and
adding the package folder to sys.path could shadow other modules. It must not import anything from ragleap_voice.

Messages are single JSON lines. The parent sends {"id", "op", ...}; the worker answers {"id", "ok", "result" or "error"}.
File descriptor 1 is redirected to stderr at start, so library prints cannot corrupt the protocol; answers go through a
saved copy of the real stdout.
"""
import json
import os
import sys
import traceback


def _send(out, obj):
    out.write(json.dumps(obj) + "\n")
    out.flush()


def serve(handlers):
    """Answer messages until a close message or the end of stdin. handlers maps an op name to a function(msg) -> dict."""
    out = os.fdopen(os.dup(1), "w", encoding="utf-8", newline="\n")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            mid, op = msg["id"], msg["op"]
        except Exception as exc:
            _send(out, {"id": None, "ok": False, "error": "unreadable message: %s" % type(exc).__name__})
            continue
        handler = handlers.get(op)
        if op == "close":
            try:
                if handler:
                    handler(msg)
            finally:
                _send(out, {"id": mid, "ok": True, "result": {}})
            return
        if handler is None:
            _send(out, {"id": mid, "ok": False, "error": "unknown op %r" % (op,)})
            continue
        try:
            result = handler(msg)
            _send(out, {"id": mid, "ok": True, "result": result or {}})
        except Exception as exc:
            traceback.print_exc()
            _send(out, {"id": mid, "ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})
