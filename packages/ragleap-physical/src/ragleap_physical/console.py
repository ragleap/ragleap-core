"""Operator console: the human side of the bridge.

The AI can only propose (through the four tools). A named person approves the exact proposal here, applies it, and can stop.
This server is NOT part of the tool set: no model tool can reach it. It listens on loopback only, needs a bearer token,
and the approver's name comes from the token table, never from the request.

Routes (JSON):
  GET  /v1/pending            proposals waiting for a person
  GET  /v1/proposals/<id>     one proposal with its digest and state
  POST /v1/approve            {"proposal_id", "digest"}: the person confirms the exact digest they saw
  POST /v1/apply              {"proposal_id"}: apply an approved proposal in simulation, then verify
  POST /v1/stop               {}: safe state, no approval needed
  POST /v1/clear-stop         {}: leave the safe state
"""
from __future__ import annotations

import hmac
import json
import re
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Mapping, Optional

from .bridge import Bridge
from .errors import (ApprovalError, CapabilityError, ConfigError, EnvelopeViolation, PhysicalError, SafetyStop,
                     ValidationError)
from .observation import actor
from ._linger import linger

MAX_BODY = 4096
MIN_TOKEN = 24
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
_ID = re.compile(r"prop_[0-9]{6,12}")
_DIGEST = re.compile(r"[0-9a-f]{64}")


def _view(bridge: Bridge, proposal) -> dict:
    d = proposal.to_dict()
    d["digest"] = proposal.digest()
    return d


def make_console(bridge: Bridge, tokens: Mapping[str, str], host: str = "127.0.0.1", port: int = 0,
                 allowed_origins=()) -> HTTPServer:
    """Build (not start) the console. ``tokens`` maps a secret token to the named person it belongs to (``person:alice``)."""
    if host not in _LOOPBACK:
        raise ConfigError("the operator console listens on loopback only")
    if not isinstance(tokens, Mapping) or not tokens:
        raise ConfigError("at least one token is needed")
    people = set()
    table = {}
    for tok, who in tokens.items():
        if not isinstance(tok, str) or len(tok) < MIN_TOKEN:
            raise ConfigError("every token needs at least %d characters" % MIN_TOKEN)
        try:
            actor(who, "approver")
        except ValidationError:
            raise ConfigError("every approver must be a safe actor id") from None
        if not who.startswith("person:"):
            raise ConfigError("every approver must be a named person (person:...)")
        if who in people:
            raise ConfigError("each person may have only one token")
        people.add(who)
        table[tok.encode()] = who
    origins = frozenset(allowed_origins)

    class Handler(BaseHTTPRequestHandler):
        timeout = 10
        server_version = "ragleap-physical-console"
        sys_version = ""

        def log_message(self, *args: Any) -> None:  # never log bodies, tokens or headers
            pass

        def _drain(self) -> None:
            """Read and discard a small unread request body before a refusal. A body larger than the cap is not read here: after the refusal is
            sent, the lingering close (see _linger.py) discards it, so the client gets the answer and not a connection reset."""
            if getattr(self, "_body_done", False):
                return
            self._body_done = True
            try:
                n = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return
            if 0 < n <= MAX_BODY:
                try:
                    self.rfile.read(n)
                    self.server.drained_bytes = getattr(self.server, "drained_bytes", 0) + n
                except Exception:
                    pass

        def _send(self, status: int, body: Optional[Mapping[str, Any]] = None, extra: Optional[dict] = None) -> None:
            if status >= 400:
                self._drain()
            data = b"" if body is None else json.dumps(body, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Connection", "close")
            if body is not None:
                self.send_header("Content-Type", "application/json")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            if status >= 400:
                # lingering close: the client can read the refusal even when it is still sending a body we did not read
                # (Windows clients report WinError 10053 when the socket is closed with unread data)
                self.server.lingered_bytes = getattr(self.server, "lingered_bytes", 0) + linger(self.connection)
                self.server.lingered_calls = getattr(self.server, "lingered_calls", 0) + 1

        def _err(self, status: int, code: str, message: str, extra: Optional[dict] = None) -> None:
            self._send(status, {"error": {"code": code, "message": message}}, extra)

        def _gate(self) -> Optional[str]:
            """Checks common to every request. Returns the approver, or None after sending the refusal."""
            port = self.server.server_address[1]
            if (self.headers.get("Host") or "") not in {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}:
                self._err(403, "host", "host not allowed")
                return None
            origin = self.headers.get("Origin")
            if origin is not None and origin not in origins:
                self._err(403, "origin", "origin not allowed")
                return None
            given = (self.headers.get("Authorization") or "").encode()
            who = None
            for tok, name in table.items():  # compare against every token: no early exit
                if hmac.compare_digest(given, b"Bearer " + tok):
                    who = name
            if who is None:
                self._err(401, "unauthorized", "unauthorized", {"WWW-Authenticate": "Bearer"})
            return who

        def _body(self) -> Optional[dict]:
            try:
                length = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                length = -1
            if length < 0:
                self._err(411, "length", "length required")
                return None
            if length > MAX_BODY:
                self._err(413, "size", "body too large")
                return None
            if length == 0:
                return {}
            if not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
                self._err(415, "type", "content type must be application/json")
                return None
            self._body_done = True
            try:
                obj = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeDecodeError, RecursionError):
                self._err(400, "parse", "parse error")
                return None
            if not isinstance(obj, dict):
                self._err(400, "shape", "body must be a JSON object")
                return None
            return obj

        def _proposal(self, obj: dict, keys: set):
            if set(obj) != keys:
                self._err(400, "keys", "unexpected or missing fields")
                return None
            pid = obj.get("proposal_id")
            if not isinstance(pid, str) or not _ID.fullmatch(pid):
                self._err(400, "proposal_id", "proposal_id is not valid")
                return None
            return bridge.get_proposal(pid)

        def _domain(self, fn) -> None:
            try:
                fn()
            except ApprovalError as exc:
                self._err(409, "approval", str(exc))
            except SafetyStop as exc:
                self._err(423, "stopped", str(exc))
            except EnvelopeViolation as exc:
                self._err(422, getattr(exc, "code", "envelope"), str(exc))
            except CapabilityError as exc:
                self._err(403, "capability", str(exc))
            except (ValidationError, PhysicalError) as exc:
                self._err(400, "invalid", str(exc))
            except Exception:  # never leak internals
                self._err(500, "internal", "internal error")

        def do_GET(self) -> None:
            if self._gate() is None:
                return
            if self.path == "/v1/pending":
                return self._domain(lambda: self._send(200, {"pending": [_view(bridge, p) for p in bridge.pending_proposals()]}))
            m = re.fullmatch(r"/v1/proposals/(prop_[0-9]{6,12})", self.path)
            if m:
                return self._domain(lambda: self._send(200, _view(bridge, bridge.get_proposal(m.group(1)))))
            self._err(404, "not_found", "not found")

        def do_POST(self) -> None:
            who = self._gate()
            if who is None:
                return
            if self.path not in {"/v1/approve", "/v1/apply", "/v1/stop", "/v1/clear-stop"}:
                return self._err(404, "not_found", "not found")
            obj = self._body()
            if obj is None:
                return
            if self.path in {"/v1/stop", "/v1/clear-stop"}:
                if obj:
                    return self._err(400, "keys", "unexpected fields")
                if self.path == "/v1/stop":
                    return self._domain(lambda: (bridge.stop(), self._send(200, {"stopped": True})))
                return self._domain(lambda: (bridge.clear_stop(), self._send(200, {"stopped": False})))

            def approve() -> None:
                p = self._proposal(obj, {"proposal_id", "digest"})
                if p is None:
                    return
                digest = obj["digest"]
                if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
                    return self._err(400, "digest", "digest is not valid")
                if not hmac.compare_digest(digest.encode(), p.digest().encode()):
                    return self._err(409, "digest_mismatch", "the digest is not the one of this proposal")
                self._send(200, {"approved": True, "approver": who, "digest": bridge.approve(p, who)})

            def apply() -> None:
                p = self._proposal(obj, {"proposal_id"})
                if p is None:
                    return
                r = bridge.apply(p)
                self._send(200, {"proposal_id": r.proposal_id, "state": r.state, "outcome_ok": r.outcome_ok,
                                 "observation_id": r.final_observation.observation_id,
                                 "proposal_digest": r.proposal_digest, "envelope_digest": r.envelope_digest})

            self._domain(approve if self.path == "/v1/approve" else apply)

        def _no(self) -> None:
            self._err(405, "method", "method not allowed", {"Allow": "GET, POST"})

        do_PUT = do_DELETE = do_PATCH = _no

    return HTTPServer((host, port), Handler)  # one request at a time: the bridge is not thread-safe
