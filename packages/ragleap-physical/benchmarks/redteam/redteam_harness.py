"""Red-team harness for ragleap-physical (issue #1205), version 2. Simulation only.

It generates seeded hostile proposals and hostile sequences and sends them through the bridge, the model-facing tools, the MCP server and the
operator console (loopback, fake adapter, fake clock), then counts how each class was refused. It ONLY REPORTS: it uses public calls, never reads
or changes a limit, and checks after every case that the envelope is unchanged.

  python benchmarks/redteam/redteam_harness.py --seed 1 --per-class 40 [--only CLASS] [--json out.json] [--include-slow]

Same seed + same package version = same report body (report_sha256). Exit code: 0 = no finding, 1 = at least one hostile case got through, a
refusal was unclean or an error occurred, 2 = a control failed (the harness itself is not trusted). Gaps that are already known are reported as
known and never counted as findings. Not covered: real thread timing (stop races are deterministic hooks), real hardware, TLS, other adapters.
"""
from __future__ import annotations

import argparse
import copy
import dataclasses
import hashlib
import http.client
import json
import platform
import random
import socket
import sys
import threading
import time
import unicodedata
from pathlib import Path

from ragleap_physical import Bridge, Capability, Envelope, ExpectedOutcome, PhysicalTools, __version__
from ragleap_physical.adapters import FakeJointAdapter
from ragleap_physical.errors import PhysicalError
from ragleap_physical.observation import actor
from ragleap_physical._linger import LINGER_MAX_BYTES
from ragleap_physical.console import MAX_BODY as CONSOLE_MAX_BODY, make_console
from ragleap_physical.mcp_server import MAX_BODY, make_server

try:  # Linux and macOS only: used to ask the kernel how many request bytes are unread when a server closes a connection
    import array
    import fcntl
    import termios
except ImportError:  # Windows
    fcntl = None

SCHEMA = "ragleap.physical.redteam/1"
CLASSES = ("bad_values", "wrong_targets", "stale_or_reused_approvals", "self_approval", "oversize_or_nested",
           "bidi_and_control", "replay_after_expiry", "stop_races", "forbidden_access",
           "console_auth", "console_approval", "console_input", "random_sequences")
CONFIG = {"envelope_version": 0, "allowed_commands": ["set_position_target"],
          "targets": {"joint_1": {"pos_min": -1.0, "pos_max": 1.0, "vel_max": 2.0, "force_max": 5.0}},
          "rate": {"max_commands_per_s": 5}, "episode": {"max_sim_time_s": 60, "max_steps": 100000},
          "on_violation": "reject"}
PROPOSER, REQUESTER, APPROVER = "employee:agent", "employee:ops", "person:alice"
TOKEN = "t" * 24
ALICE_T, BOB_T = "a" * 24, "b" * 24
CON_TOKENS = {ALICE_T: "person:alice", BOB_T: "person:bob"}
INVARIANTS_FILE = Path(__file__).with_name("invariants.json")  # class -> invariant ids, copied from docs/safety-invariants.md

# Gaps that are already known and marked as expected failures in the repository: reported as known, never counted as new findings.
KNOWN_GAPS = {
    "#1339": "a stop from another thread can land between the Bridge's last check and the adapter call; the adapters do not refuse a command while stopped",
    "#1367": "the console's stop waits behind a client that announced a body and never sent it (probed only with --include-slow: about 10 s)",
    "linger-limit": "a refused body above the 1 MiB linger limit is still left partly unread (probed on Linux only)",
}
BIDI = [0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069, 0x200E, 0x200F, 0x061C, 0x200B,
        0x2060, 0xFEFF, 0x00AD, 0x2028, 0x2029, 0x85, 0x1B, 0x7F, 0x00, 0x07, 0x0A, 0x0D, 0x09, 0x9F, 0xE0001,
        0xE0041, 0xD800]


def good_args() -> dict:
    return {"target": "joint_1", "command": "set_position_target", "value": 0.5, "unit": "rad", "reason": "check the joint",
            "expected_outcome": {"entity": "joint_1", "quantity": "pos", "lo": 0.45, "hi": 0.55, "unit": "rad",
                                 "by_sim_time_s": 5.0}}


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class HookedJoint(FakeJointAdapter):
    """Fake joint that can call a hook at the n-th call of one method: deterministic stop races, no threads."""

    def __init__(self) -> None:
        super().__init__()
        self.counts = {"dry_run": 0, "advance": 0, "apply": 0}
        self.trigger = None  # (method, n)
        self.on_trigger = None
        self.fired = False
        self.stop_requested = False
        self.late_applies = 0

    def _tick_hook(self, method: str) -> None:
        self.counts[method] += 1
        if self.trigger == (method, self.counts[method]) and self.on_trigger:
            self.fired = True
            self.on_trigger()

    def dry_run(self, *a, **k):
        r = super().dry_run(*a, **k)
        self._tick_hook("dry_run")
        return r

    def apply(self, *a, **k):
        if self.trigger == ("apply_entry", self.counts["apply"] + 1) and self.on_trigger:
            self.fired = True
            self.on_trigger()
        if self.stop_requested:
            self.late_applies += 1
        super().apply(*a, **k)
        self._tick_hook("apply")

    def advance(self, *a, **k):
        r = super().advance(*a, **k)
        self._tick_hook("advance")
        return r


def default_factory(adapter, env, clock, level, kw):
    return Bridge(adapter, env, org_id="org_demo", max_capability=level, clock=clock, **kw)


class World:
    """One fresh bridge (+ tools, + optional loopback MCP server) per hostile case."""

    def __init__(self, factory, *, adapter=None, level=Capability.CONTROL, server=False, token=None, console=False, **kw) -> None:
        self.clock, self.adapter = Clock(), adapter or HookedJoint()
        self.bridge = factory(self.adapter, Envelope(copy.deepcopy(CONFIG)), self.clock, level, kw)
        self.tools = PhysicalTools(self.bridge, proposer=PROPOSER)
        self.limits0 = self.bridge.envelope_view()
        self.server = self.thread = self.console = self.cthread = None
        if server:
            self.server = make_server(self.tools, "127.0.0.1", 0, auth_token=token)
            self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            self.thread.start()
        if console:
            self.console = make_console(self.bridge, CON_TOKENS)
            self.cthread = threading.Thread(target=self.console.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            self.cthread.start()

    def limits_unchanged(self) -> bool:
        return self.bridge.envelope_view() == self.limits0 == CONFIG_VIEW

    def leaked(self) -> bool:
        """A hostile proposal is held by the bridge, or a command reached the device."""
        return bool(self.bridge.pending_proposals()) or bool(self.adapter.applied)

    def post(self, body, headers=None, path="/mcp", method="POST"):
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        try:
            conn.request(method, path, body=body, headers=h)
            resp = conn.getresponse()
            raw, status = resp.read(), resp.status
        except (OSError, http.client.HTTPException):
            return None, None
        finally:
            conn.close()
        try:
            return status, (json.loads(raw) if raw else None)
        except ValueError:
            return status, None

    def close(self) -> None:
        for srv, th in ((self.server, self.thread), (self.console, self.cthread)):
            if srv:
                srv.shutdown()
                srv.server_close()
                th.join(2)


CONFIG_VIEW = Envelope(copy.deepcopy(CONFIG)).as_dict()


def rpc_call(name, arguments) -> bytes:
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    return json.dumps(msg, allow_nan=True).encode()


def verdict_of_exception(exc: BaseException):
    return ("refused", getattr(exc, "code", None) or type(exc).__name__) if isinstance(exc, PhysicalError) else ("unclean", type(exc).__name__)


def attempt(fn):
    """Run one hostile step. ('accepted', None) if it returned, else refused (typed error) or unclean (any other error)."""
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - the harness must survive anything a hostile input causes
        return verdict_of_exception(exc)
    return "accepted", None


def tool_verdict(result: dict):
    if result.get("ok"):
        return "accepted", None
    code = (result.get("error") or {}).get("code", "error")
    return ("unclean", code) if code == "internal_error" else ("refused", code)


def mcp_verdict(status, parsed):
    if status is None:
        return "unclean", "no_answer"
    if status == 200 and isinstance(parsed, dict):
        try:
            if "error" in parsed:
                return "refused", "rpc_%s" % parsed["error"].get("code")
            return tool_verdict(json.loads(parsed["result"]["content"][0]["text"]))
        except (KeyError, IndexError, TypeError, ValueError):
            return "unclean", "bad_reply"
    if status == 500:
        return "unclean", "http_500"
    return "refused", "http_%d" % status


class Recorder:
    def __init__(self, seed: int) -> None:
        self.seed, self.rows, self.findings, self.known, self.n = seed, [], [], {}, {}

    def next_case(self, cls: str) -> int:
        self.n[cls] = self.n.get(cls, -1) + 1
        return self.n[cls]

    def add(self, cls, idx, scenario, layer, verdict, code, shown="", world=None, known=None, trace=None):
        if world is not None and not world.limits_unchanged():
            verdict, code = "error", "limits_changed"
        if verdict == "passed" and known:
            verdict = "known"
        self.rows.append((cls, scenario, layer, verdict, str(code)))
        row = {"seed": self.seed, "class": cls, "case": idx, "scenario": scenario, "layer": layer, "detail": str(code), "input": shown[:100]}
        if trace:
            row["trace"] = trace
        if verdict == "known":
            self.known.setdefault(known, []).append(row)
        elif verdict in ("passed", "error"):
            row["verdict"] = verdict
            self.findings.append(row)


def short(v) -> str:
    return repr(v)[:60]


# ---------------------------------------------------------------- proposals through the three layers
def bridge_propose(world: World, args: dict, requested_by: str = REQUESTER):
    o = args["expected_outcome"]
    return world.bridge.propose(requested_by=requested_by, target=args["target"], command=args["command"], value=args["value"],
                                unit=args["unit"], reason=args.get("reason", ""),
                                expected_outcome=ExpectedOutcome(o["entity"], o["quantity"], o["lo"], o["hi"], o["unit"],
                                                                 o["by_sim_time_s"]))


def through_layers(rec, cls, idx, scenario, args, factory, *, requested_by=REQUESTER, shown="", layers=("bridge", "tools", "mcp")):
    """Send one hostile argument set through bridge, tools and the MCP server; each on a fresh world."""
    if "bridge" in layers:
        w = World(factory)
        v, c = attempt(lambda: bridge_propose(w, args, requested_by))
        rec.add(cls, idx, scenario, "bridge", "passed" if (v == "accepted" or w.leaked()) else v, c, shown, w)
    if "tools" in layers:
        w = World(factory)
        v, c = tool_verdict(w.tools.call("physical_propose_action", copy.deepcopy(args)))
        rec.add(cls, idx, scenario, "tools", "passed" if (v == "accepted" or w.leaked()) else v, c, shown, w)
    if "mcp" in layers:
        w = World(factory, server=True)
        try:
            v, c = mcp_verdict(*w.post(rpc_call("physical_propose_action", args)))
            rec.add(cls, idx, scenario, "mcp", "passed" if (v == "accepted" or w.leaked()) else v, c, shown, w)
        finally:
            w.close()


# ---------------------------------------------------------------- classes
def c_bad_values(rec, rng, n, factory):
    cls = "bad_values"
    for _ in range(n):
        idx = rec.next_case(cls)
        kind = rng.choice(["nan", "inf", "ninf", "text", "bool", "none", "bigint", "list", "dict", "range"])
        sign = rng.choice([1, -1])
        value = {"nan": float("nan"), "inf": float("inf"), "ninf": float("-inf"),
                 "text": rng.choice(["0.5", "abc", "", "1e0", "NaN"]), "bool": rng.choice([True, False]), "none": None,
                 "bigint": sign * 10 ** rng.randint(309, 500), "list": [0.5], "dict": {"v": 0.5},
                 "range": sign * (1.0 + rng.choice([1e-9, 1e-6, 1e-3, 0.1, 1, 100, 1e6, 1e300]))}[kind]
        args = good_args()
        args["value"] = value
        through_layers(rec, cls, idx, kind, args, factory, shown=short(value))


def c_wrong_targets(rec, rng, n, factory):
    cls = "wrong_targets"
    bad_ids = ["joint_2", "Joint_1", "joint_1 ", "", "../joint_1", "joint_1\n", "joint-1", "j" * 65, "j" * 200, None, 5,
               "joint_1;rm", "JOINT_1", "joint_1\x00", "joint_1\u202e", "base"]
    for _ in range(n):
        idx = rec.next_case(cls)
        field = rng.choice(["target", "command", "unit", "outcome_entity", "outcome_time", "outcome_quantity",
                            "outcome_bounds", "requested_by"])
        args, requested_by = good_args(), REQUESTER
        if field == "target":
            args["target"] = shown = rng.choice(bad_ids)
        elif field == "command":
            args["command"] = shown = rng.choice(["set_velocity_target", "apply", "set_tool_offset", "SET_POSITION_TARGET", "", None,
                                                  "set_position_target\n", "stop"])
        elif field == "unit":
            args["unit"] = shown = rng.choice(["deg", "m", "rad/s", "", None, "RAD", "um", "rad\n"])
        elif field == "outcome_entity":
            args["expected_outcome"]["entity"] = shown = rng.choice(["joint_2", "Joint_1", "", "base", "joint_1 "])
        elif field == "outcome_time":
            args["expected_outcome"]["by_sim_time_s"] = shown = rng.choice([60.0001, 61, 1e6, 1e300, 0, -1, -1e9])
        elif field == "outcome_quantity":
            args["expected_outcome"]["quantity"] = shown = rng.choice(["acc", "POS", "", "force"])
        elif field == "outcome_bounds":
            args["expected_outcome"]["lo"], args["expected_outcome"]["hi"] = 0.9, 0.1
            shown = "lo>hi"
        else:
            requested_by = shown = rng.choice(["", "a" * 65, "person:al ice", "x\n", None, 7, "emp\u202eloyee", "a/b"])
        through_layers(rec, cls, idx, field, args, factory, requested_by=requested_by, shown=short(shown),
                       layers=("bridge",) if field == "requested_by" else ("bridge", "tools", "mcp"))  # requested_by is fixed by the tool layer


def _reset_sim(w: World) -> None:
    """Restart the simulated device (harness side, not a limit). Without it a replay is also stopped by the 'outcome time is
    already past' check, which would hide a broken single-use rule behind a second guard."""
    a = w.adapter
    a.pos, a.vel, a.step, a.t, a.target, a.stopped = 0.0, 0.0, 0, 0.0, 0.0, False


def _approved(w: World, value=0.5, requested_by=REQUESTER, by=5.0):
    args = good_args()
    args["value"] = value
    args["expected_outcome"].update(lo=value - 0.05, hi=value + 0.05, by_sim_time_s=by)
    p = bridge_propose(w, args, requested_by)
    w.bridge.approve(p, APPROVER)
    return p


def _check(rec, cls, idx, scenario, w, shown, steps):
    """steps: (call, commands already allowed on the device, decisive). A decisive call that is accepted, or any call after which
    more commands reached the device than allowed, makes the case 'passed' (a hostile act got through)."""
    final, code = "refused", "-"
    for step in steps:
        fn, expected_applied = step[0], step[1]
        decisive = step[2] if len(step) > 2 else True
        v, c = attempt(fn)
        if (v == "accepted" and decisive) or len(w.adapter.applied) > expected_applied:
            final, code = "passed", ("accepted" if v == "accepted" else "device_command")
            break
        if v == "unclean":
            final, code = "unclean", c
        elif final != "unclean":
            code = c
    rec.add(cls, idx, scenario, "bridge", final, code, shown, w)


def c_stale_or_reused(rec, rng, n, factory):
    cls = "stale_or_reused_approvals"
    for _ in range(n):
        idx = rec.next_case(cls)
        sc = rng.choice(["apply_unapproved", "apply_twice", "apply_twice_after_sim_reset", "approve_twice", "approve_after_apply", "stale_approval",
                         "tamper_after_approval", "tamper_before_approval", "forged_proposal", "cross_bridge", "approval_before_stop"])
        value = round(rng.uniform(-0.9, 0.9), 3)
        w = World(factory, ttl_s=float(rng.randint(1, 600)))
        try:
            if sc == "apply_unapproved":
                p = bridge_propose(w, {**good_args(), "value": value, "expected_outcome": {**good_args()["expected_outcome"], "lo": value - .05, "hi": value + .05}})
                _check(rec, cls, idx, sc, w, short(value), [(lambda: w.bridge.apply(p), 0)])
            elif sc in ("apply_twice", "apply_twice_after_sim_reset", "approve_twice", "approve_after_apply"):
                p = _approved(w, value)
                if sc == "approve_twice":
                    _check(rec, cls, idx, sc, w, short(value), [(lambda: w.bridge.approve(p, APPROVER), 0)])
                else:
                    r = w.bridge.apply(p)
                    assert r.state == "verified"
                    if sc == "apply_twice_after_sim_reset":
                        _reset_sim(w)
                    step = (lambda: w.bridge.apply(p)) if sc.startswith("apply_twice") else (lambda: w.bridge.approve(p, APPROVER))
                    _check(rec, cls, idx, sc, w, short(value), [(step, 1)])
            elif sc == "approval_before_stop":  # a stop invalidates every approval given before it (#1338)
                p = _approved(w, value)
                _request_stop(w)
                w.bridge.clear_stop()
                w.adapter.stop_requested = False
                _check(rec, cls, idx, sc, w, short(value), [(lambda: w.bridge.apply(p), 0)])
            elif sc == "stale_approval":
                p = _approved(w, value)
                w.clock.t += 601 + rng.choice([0.001, 1, 3600, 1e6])
                _check(rec, cls, idx, sc, w, "after +%s" % w.clock.t, [(lambda: w.bridge.apply(p), 0)])
            elif sc == "tamper_after_approval":
                p = _approved(w, value)
                bad = dataclasses.replace(p, value=rng.choice([value, 0.99, -0.99, 0.0]), reason=rng.choice(["changed", p.reason]))
                if bad.digest() == p.digest():
                    bad = dataclasses.replace(p, requested_by="person:mallory")
                _check(rec, cls, idx, sc, w, short(bad.value), [(lambda: w.bridge.apply(bad), 0)])
            elif sc == "tamper_before_approval":
                args = {**good_args(), "value": value, "expected_outcome": {**good_args()["expected_outcome"], "lo": value - .05, "hi": value + .05}}
                p = bridge_propose(w, args)
                bad = dataclasses.replace(p, value=0.99 if value < 0.9 else -0.99)
                _check(rec, cls, idx, sc, w, short(bad.value), [(lambda: w.bridge.approve(bad, APPROVER), 0), (lambda: w.bridge.apply(bad), 0)])
            elif sc == "forged_proposal":
                good = _approved(w, value)
                forged = dataclasses.replace(good, proposal_id="prop_%06d" % rng.randint(2, 999999))
                _check(rec, cls, idx, sc, w, forged.proposal_id, [(lambda: w.bridge.approve(forged, APPROVER), 0), (lambda: w.bridge.apply(forged), 0)])
            else:  # cross_bridge: an approval on bridge A is used on bridge B (same ids, different content)
                pa = _approved(w, value)
                w2 = World(factory, ttl_s=300.0)
                try:
                    _approved(w2, 0.1 if value != 0.1 else 0.2)
                    _check(rec, cls, idx, sc, w2, pa.proposal_id, [(lambda: w2.bridge.apply(pa), 0)])
                finally:
                    w2.close()
        except Exception as exc:  # setup of the scenario failed: report it, never hide it
            rec.add(cls, idx, sc, "bridge", "error", "setup_" + type(exc).__name__, "", None)
        finally:
            w.close()


def c_self_approval(rec, rng, n, factory):
    cls = "self_approval"
    bad_approvers = ["", "a" * 65, "person:alice\n", "person:al ice", None, 7, "\u202eperson:alice", "person:alice\x00", "a/b", "per son", "x" * 200]
    for _ in range(n):
        idx = rec.next_case(cls)
        sc = rng.choice(["exact", "exact", "invalid_approver", "via_tools", "case_variant", "case_variant"])
        w = World(factory)
        try:
            if sc == "exact":
                who = rng.choice([REQUESTER, "person:alice", "employee:agent", "bot", "x_%d" % rng.randint(0, 9999)])
                p = bridge_propose(w, good_args(), who)
                _check(rec, cls, idx, sc, w, who, [(lambda: w.bridge.approve(p, who), 0), (lambda: w.bridge.apply(p), 0)])
            elif sc == "invalid_approver":
                bad = rng.choice(bad_approvers)
                p = bridge_propose(w, good_args())
                _check(rec, cls, idx, sc, w, short(bad), [(lambda: w.bridge.approve(p, bad), 0), (lambda: w.bridge.apply(p), 0)])
            elif sc == "case_variant":  # the same person written with other letter case (fixed by #1337)
                who = rng.choice([REQUESTER, "person:alice", "person:Bob", "employee:agent"])
                variant = rng.choice([who.upper(), who.swapcase(), who.title(), who.lower()])
                if variant == who:
                    variant = who.upper() if who.upper() != who else who.swapcase()
                p = bridge_propose(w, good_args(), who)
                _check(rec, cls, idx, sc, w, "%s as %s" % (who, variant), [(lambda: w.bridge.approve(p, variant), 0), (lambda: w.bridge.apply(p), 0)])
            else:  # the model-facing tool asks; the proposer then tries to approve its own proposal
                res = w.tools.call("physical_propose_action", good_args())
                assert res["ok"], res
                p = w.bridge.get_proposal(res["data"]["proposal"]["proposal_id"])
                _check(rec, cls, idx, sc, w, PROPOSER, [(lambda: w.bridge.approve(p, PROPOSER), 0), (lambda: w.bridge.apply(p), 0)])
        except Exception as exc:
            rec.add(cls, idx, sc, "bridge", "error", "setup_" + type(exc).__name__, "", None)
        finally:
            w.close()


def _body_with(args, **extra) -> dict:
    a = copy.deepcopy(args)
    a.update(extra)
    return a


def c_oversize_nested(rec, rng, n, factory):
    cls = "oversize_or_nested"
    for _ in range(n):
        idx = rec.next_case(cls)
        sc = rng.choice(["pad", "deep_value", "deep_arguments", "deep_dict", "long_reason", "long_target", "extra_key", "missing_key",
                         "wrong_type", "args_not_object", "batch", "garbage", "bad_utf8", "many_keys"])
        args, byte_body, shown = good_args(), None, sc
        if sc == "pad":
            body = rpc_call("physical_propose_action", args)
            size = rng.randint(MAX_BODY + 1, 200000)
            byte_body, shown = body + b" " * (size - len(body)), "size %d" % size
        elif sc in ("deep_value", "deep_arguments", "deep_dict"):
            d = rng.randint(100, 20000)
            nest = "[" * d + "]" * d if sc != "deep_dict" else '{"a":' * (d // 2) + "1" + "}" * (d // 2)
            pre = '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"physical_propose_action","arguments":'
            inner = json.dumps(args)
            if sc == "deep_value":
                inner = inner.replace('"value": 0.5', '"value": ' + nest)
            elif sc == "deep_arguments":
                inner = nest
            else:
                inner = inner[:-1] + ', "x": ' + nest + "}"
            byte_body, shown = (pre + inner + "}}").encode(), "depth %d" % d
        elif sc == "long_reason":
            args["reason"], shown = "a" * rng.randint(501, 20000), "long reason"
        elif sc == "long_target":
            args["target"], shown = "j" * rng.randint(201, 3000), "long target"
        elif sc == "extra_key":
            key = rng.choice(["requested_by", "approver", "approved", "dry_run", "capability", "execute", "envelope", "proposal_id"])
            args[key], shown = rng.choice([True, "person:alice", {"x": 1}, 1]), key
        elif sc == "missing_key":
            key = rng.choice(["target", "command", "value", "unit", "expected_outcome"])
            del args[key]
            shown = "no " + key
        elif sc == "wrong_type":
            key = rng.choice(["target", "command", "value", "unit", "reason", "expected_outcome"])
            wrong = {"value": ["0.5", [1], None, {"a": 1}, True], "expected_outcome": [5, "0.5", [1], None, True]}
            args[key], shown = rng.choice(wrong.get(key, [5, [1], None, {"a": 1}, True])), "type " + key  # never a valid type for the field
        elif sc == "args_not_object":
            args, shown = rng.choice([[1, 2], "x", 7, [args]]), "arguments not an object"
        elif sc == "many_keys":
            args.update({"k%d" % i: i for i in range(rng.randint(500, 2000))})
        if sc in ("batch", "garbage", "bad_utf8"):
            byte_body = {"batch": json.dumps([json.loads(rpc_call("physical_propose_action", args))]).encode(),
                         "garbage": bytes(rng.randrange(256) for _ in range(rng.randint(1, 500))),
                         "bad_utf8": b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"\xff"}}'}[sc]
        if byte_body is None:  # argument-level hostile input: tools layer and MCP layer
            through_layers(rec, cls, idx, sc, args, factory, shown=shown) if sc not in ("missing_key", "wrong_type", "extra_key", "args_not_object", "many_keys") else _args_only(rec, cls, idx, sc, args, factory, shown)
        else:
            w = World(factory, server=True)
            try:
                v, c = mcp_verdict(*w.post(byte_body))
                rec.add(cls, idx, sc, "mcp", "passed" if (v == "accepted" or w.leaked()) else v, c, shown, w)
            finally:
                w.close()


def _args_only(rec, cls, idx, sc, args, factory, shown):
    """Shapes the bridge cannot even receive (extra keys, missing keys): tools layer and MCP only."""
    w = World(factory)
    v, c = tool_verdict(w.tools.call("physical_propose_action", copy.deepcopy(args)))
    rec.add(cls, idx, sc, "tools", "passed" if (v == "accepted" or w.leaked()) else v, c, shown, w)
    w = World(factory, server=True)
    try:
        v, c = mcp_verdict(*w.post(rpc_call("physical_propose_action", args)))
        rec.add(cls, idx, sc, "mcp", "passed" if (v == "accepted" or w.leaked()) else v, c, shown, w)
    finally:
        w.close()


def c_bidi(rec, rng, n, factory):
    cls = "bidi_and_control"
    for _ in range(n):
        idx = rec.next_case(cls)
        ch = chr(rng.choice(BIDI))
        text = "".join(rng.choice("abc xyz") for _ in range(rng.randint(0, 6)))
        cut = rng.randint(0, len(text))
        evil = text[:cut] + ch + text[cut:]
        field = rng.choice(["reason", "reason", "target", "requested_by", "approver"])
        shown = "%s U+%04X" % (field, ord(ch))
        if field in ("reason", "target"):
            args = good_args()
            args[field] = evil if field == "reason" else "joint_1" + ch
            through_layers(rec, cls, idx, field, args, factory, shown=shown)
        else:
            w = World(factory)
            try:
                if field == "requested_by":
                    _check(rec, cls, idx, field, w, shown, [(lambda: bridge_propose(w, good_args(), "employee:" + evil), 0)])
                    w.adapter.applied.clear()
                else:
                    p = bridge_propose(w, good_args())
                    _check(rec, cls, idx, field, w, shown, [(lambda: w.bridge.approve(p, "person:" + evil), 0), (lambda: w.bridge.apply(p), 0)])
            except Exception as exc:
                rec.add(cls, idx, field, "bridge", "error", "setup_" + type(exc).__name__, "", None)
            finally:
                w.close()


def c_replay(rec, rng, n, factory):
    cls = "replay_after_expiry"
    for _ in range(n):
        idx = rec.next_case(cls)
        sc = rng.choice(["replay_after_expiry", "approve_expired", "clock_rewound", "evicted", "failed_replay"])
        ttl = float(rng.randint(1, 600))
        w = World(factory, ttl_s=ttl, **({"max_records": 3} if sc == "evicted" else {}))
        try:
            if sc == "replay_after_expiry":
                p = _approved(w)
                assert w.bridge.apply(p).state == "verified"
                _reset_sim(w)
                w.clock.t += ttl + rng.choice([0.001, 1, 1e6])
                _check(rec, cls, idx, sc, w, "after ttl", [(lambda: w.bridge.apply(p), 1)])
            elif sc == "approve_expired":
                p = bridge_propose(w, good_args())
                w.clock.t += ttl + rng.choice([0.001, 1, 1e6])
                _check(rec, cls, idx, sc, w, "after ttl", [(lambda: w.bridge.approve(p, APPROVER), 0), (lambda: w.bridge.apply(p), 0)])
            elif sc == "clock_rewound":
                p = _approved(w)
                assert w.bridge.apply(p).state == "verified"
                _reset_sim(w)
                w.clock.t -= rng.choice([1.0, 100.0, 1e6])
                _check(rec, cls, idx, sc, w, "clock back", [(lambda: w.bridge.apply(p), 1)])
            elif sc == "evicted":
                p = _approved(w)
                assert w.bridge.apply(p).state == "verified"
                for k in range(5):
                    bridge_propose(w, {**good_args(), "value": 0.1 * (k + 1), "expected_outcome": {**good_args()["expected_outcome"], "lo": 0.1 * (k + 1) - .05, "hi": 0.1 * (k + 1) + .05}})
                _check(rec, cls, idx, sc, w, "after eviction", [(lambda: w.bridge.apply(p), 1), (lambda: w.bridge.approve(p, APPROVER), 1)])
            else:  # a proposal whose outcome check failed is replayed
                args = good_args()
                args["expected_outcome"].update(lo=5.0, hi=6.0)
                p = bridge_propose(w, args)
                w.bridge.approve(p, APPROVER)
                r = w.bridge.apply(p)
                assert r.state == "failed"
                _check(rec, cls, idx, sc, w, "failed proposal", [(lambda: w.bridge.apply(p), 1)])
        except Exception as exc:
            rec.add(cls, idx, sc, "bridge", "error", "setup_" + type(exc).__name__, "", None)
        finally:
            w.close()


def _request_stop(w: World) -> None:
    w.adapter.stop_requested = True
    w.bridge.stop()


def c_stop_races(rec, rng, n, factory):
    cls = "stop_races"
    for _ in range(n):
        idx = rec.next_case(cls)
        sc = rng.choice(["stop_before_apply", "stop_before_approve", "stop_in_dry_run", "stop_in_advance", "stop_in_advance", "stop_at_adapter_call"])
        w = World(factory)
        try:
            if sc == "stop_before_apply":
                p = _approved(w)
                _request_stop(w)
                _check(rec, cls, idx, sc, w, sc, [(lambda: w.bridge.apply(p), 0)])
            elif sc == "stop_before_approve":
                p = bridge_propose(w, good_args())
                _request_stop(w)
                _check(rec, cls, idx, sc, w, sc, [(lambda: w.bridge.approve(p, APPROVER), 0, False), (lambda: w.bridge.apply(p), 0)])
            elif sc == "stop_at_adapter_call":  # a stop lands after the Bridge's last check, at the entry of the adapter call: known gap #1339
                p = _approved(w)
                w.adapter.trigger, w.adapter.on_trigger = ("apply_entry", 1), (lambda: _request_stop(w))
                v, c = attempt(lambda: w.bridge.apply(p))
                if not w.adapter.fired:
                    rec.add(cls, idx, sc, "bridge", "inconclusive", "trigger_not_reached", sc, w)
                elif v == "accepted":
                    rec.add(cls, idx, sc, "bridge", "passed", "returned_normally", sc, w)
                elif w.adapter.late_applies:
                    rec.add(cls, idx, sc, "bridge", "passed", "device_command_after_stop_request", sc, w, known="#1339")
                else:
                    rec.add(cls, idx, sc, "bridge", "refused", "adapter_refused_while_stopped", sc, w)
            else:
                p = _approved(w)
                k = 1 if sc == "stop_in_dry_run" else rng.randint(1, 20)
                w.adapter.trigger = ("dry_run" if sc == "stop_in_dry_run" else "advance", k)
                w.adapter.on_trigger = lambda: _request_stop(w)
                v, c = attempt(lambda: w.bridge.apply(p))
                shown = "%s k=%d" % (sc, k)
                if not w.adapter.fired:
                    rec.add(cls, idx, sc, "bridge", "inconclusive", "trigger_not_reached", shown, w)
                    continue
                if v == "accepted" or w.adapter.late_applies:
                    rec.add(cls, idx, sc, "bridge", "passed", "returned_normally" if v == "accepted" else "device_command_after_stop_request", shown, w)
                elif not w.adapter.stopped:
                    rec.add(cls, idx, sc, "bridge", "passed", "adapter_not_held", shown, w)
                else:
                    ok = attempt(lambda: w.bridge.observe())[0] == "accepted"
                    rec.add(cls, idx, sc, "bridge", "refused" if ok else "error", c if ok else "observe_failed_after_stop", shown, w)
        except Exception as exc:
            rec.add(cls, idx, sc, "bridge", "error", "setup_" + type(exc).__name__, "", None)
        finally:
            w.close()


def c_forbidden(rec, rng, n, factory):
    cls = "forbidden_access"
    names = ["physical_approve", "physical_apply", "physical_stop", "physical_clear_stop", "physical_set_envelope", "approve", "stop",
             "apply_proposal", "physical_propose_action\n", "Physical_Propose_Action", "physical_propose_act\u0456on", " physical_get_observation",
             "physical_get_observation\x00", "", None, 5, ["physical_get_observation"]]
    methods = ["resources/read", "prompts/list", "sampling/createMessage", "logging/setLevel", "tools/call ", "Tools/List", "shutdown",
               "admin/stop", 7, None, ["tools/list"]]
    for _ in range(n):
        idx = rec.next_case(cls)
        sc = rng.choice(["tool_name", "tool_name", "method", "token", "host", "origin", "path", "http_method", "content_type"])
        valid = rpc_call("physical_propose_action", good_args())
        headers, path, method, token, body, shown = {}, "/mcp", "POST", TOKEN if sc == "token" else None, valid, sc
        if sc == "token":
            tk = rng.choice(["", "x", TOKEN[:-1], TOKEN + "x", TOKEN.upper(), " " + TOKEN, "bearer " + TOKEN, "Bearer", "Bearer ", "Bearer " + TOKEN[:-1]])
            if rng.random() >= 0.3:  # otherwise: no Authorization header at all
                headers["Authorization"] = rng.choice(["", "Bearer x", "Bearer " + TOKEN[:-1], "Bearer " + TOKEN + "x", "Bearer " + TOKEN.upper(),
                                                       "Bearer  " + TOKEN, "bearer " + TOKEN, "Bearer", "Bearer ", TOKEN])
            shown = "auth " + short(headers.get("Authorization", "none"))
        elif sc == "tool_name":
            nm = rng.choice(names)
            body, shown = rpc_call(nm, {"proposal_id": "prop_000001", "approver": APPROVER}), "tool " + short(nm)
        elif sc == "method":
            m = rng.choice(methods)
            body, shown = json.dumps({"jsonrpc": "2.0", "id": 1, "method": m, "params": {}}).encode(), "method " + short(m)
        elif sc == "host":
            headers["Host"] = rng.choice(["evil.example", "evil.example:80", "127.0.0.1", "localhost", "[::2]:1", "127.0.0.1.evil.example:1", ""])
            shown = "host " + short(headers["Host"])
        elif sc == "origin":
            headers["Origin"] = rng.choice(["http://evil.example", "null", "http://127.0.0.1", "", "*"])
            shown = "origin " + short(headers["Origin"])
        elif sc == "path":
            path = rng.choice(["/", "/mcp/", "/MCP", "/mcp?x=1", "/mcp/../mcp", "/admin", "/mcp%00", "/mcp;x"])
            shown = "path " + path
        elif sc == "http_method":
            method, body = rng.choice(["GET", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"]), None
            shown = method
        else:
            headers["Content-Type"] = rng.choice(["text/plain", "", "application/x-www-form-urlencoded", "multipart/form-data"])
            shown = "type " + short(headers["Content-Type"])
        w = World(factory, server=True, token=token)
        try:
            v, c = mcp_verdict(*w.post(body, headers, path, method))
            rec.add(cls, idx, sc, "mcp", "passed" if (v == "accepted" or w.leaked()) else v, c, shown, w)
        finally:
            w.close()
    # the tool list itself must show exactly the four tools and nothing that approves, applies or stops
    w = World(factory, server=True)
    try:
        idx = rec.next_case(cls)
        status, parsed = w.post(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode())
        listed = sorted(t["name"] for t in parsed["result"]["tools"]) if status == 200 and parsed else None
        ok = listed == ["physical_get_envelope", "physical_get_observation", "physical_list_sources", "physical_propose_action"]
        rec.add(cls, idx, "tool_list_exact", "mcp", "refused" if ok else "passed", "exact" if ok else str(listed), "tools/list", w)
    finally:
        w.close()


# ---------------------------------------------------------------- the operator console as a target
def http_call(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        raw, status = resp.read(), resp.status
    except (OSError, http.client.HTTPException):
        return None, None
    finally:
        conn.close()
    try:
        return status, (json.loads(raw) if raw else None)
    except ValueError:
        return status, None


def status_verdict(status):
    """Status codes only: the reason phrases belong to the standard library and change between Python versions."""
    if status is None:
        return "unclean", "no_answer"
    if status >= 500:
        return "unclean", "http_%d" % status
    if 200 <= status < 300:
        return "accepted", None
    return "refused", "http_%d" % status


def raw_status(port, data: bytes):
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    try:
        s.sendall(data)
        first = b""
        while b"\r\n" not in first:
            chunk = s.recv(4096)
            if not chunk:
                break
            first += chunk
        parts = first.split(b" ", 2)
        return int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    except OSError:
        return None
    finally:
        s.close()


def creq(w, method, path, obj=None, *, token=ALICE_T, headers=None, raw=None, ctype="application/json"):
    h = {}
    if token is not None:
        h["Authorization"] = "Bearer " + token
    body = raw if raw is not None else (json.dumps(obj, allow_nan=True).encode() if obj is not None else None)
    if body is not None:
        h["Content-Type"] = ctype
    h.update(headers or {})
    return http_call(w.console.server_address[1], method, path, body, h)


def approve_body(p, digest=None):
    return {"proposal_id": p.proposal_id, "digest": p.digest() if digest is None else digest}


def hostile_status(rec, cls, idx, sc, w, status, shown, known=None):
    v, c = status_verdict(status)
    rec.add(cls, idx, sc, "console", "passed" if v == "accepted" else v, c, shown, w, known=known)


def c_console_auth(rec, rng, n, factory):
    cls = "console_auth"
    bad_auth = [None, "", "Bearer", "Bearer ", "bearer " + ALICE_T, "BEARER " + ALICE_T, "Basic " + ALICE_T, "Token " + ALICE_T, ALICE_T,
                "Bearer " + ALICE_T + "x", "Bearer " + ALICE_T[:-1], "Bearer  " + ALICE_T, "Bearer " + "c" * 24, "Bearer " + "c" * 100]
    paths = [("GET", "/v1/approve"), ("GET", "/v1/apply"), ("GET", "/v1/stop"), ("GET", "/v1/clear-stop"), ("POST", "/v1/pending"), ("PUT", "/v1/stop"),
             ("DELETE", "/v1/pending"), ("PATCH", "/v1/stop"), ("POST", "/v1/proposals/prop_000001"), ("GET", "/v1/proposals/../pending"),
             ("GET", "/v1/proposals/prop_000001/x"), ("GET", "/v1/admin"), ("POST", "/v1/"), ("POST", "/v1/stop/"), ("POST", "/v1/STOP"),
             ("GET", "/v1/pending?x=1"), ("POST", "/v1/approve;x"), ("GET", "/admin"), ("GET", "/")]
    for _ in range(n):
        idx = rec.next_case(cls)
        sc = rng.choice(["bad_auth", "bad_auth", "bad_auth", "host", "origin", "method_path"])
        w = World(factory, console=True)
        try:
            route = rng.choice(["approve", "apply", "stop", "clear", "pending", "view"])
            p = bridge_propose(w, good_args(), PROPOSER)
            if route == "apply":
                w.bridge.approve(p, APPROVER)
            if route == "clear":
                w.bridge.stop()
            method, path, obj = {"approve": ("POST", "/v1/approve", approve_body(p)), "apply": ("POST", "/v1/apply", {"proposal_id": p.proposal_id}),
                                 "stop": ("POST", "/v1/stop", {}), "clear": ("POST", "/v1/clear-stop", {}), "pending": ("GET", "/v1/pending", None),
                                 "view": ("GET", "/v1/proposals/" + p.proposal_id, None)}[route]
            headers, token, shown = {}, ALICE_T, route
            if sc == "bad_auth":
                auth = rng.choice(bad_auth)
                token = None
                if auth is not None:
                    headers["Authorization"] = auth
                shown = "%s auth %s" % (route, short(auth))
            elif sc == "host":
                headers["Host"] = rng.choice(["evil.example", "evil.example:80", "127.0.0.1", "localhost", "[::2]:1", "127.0.0.1.evil.example:1", ""])
                shown = "%s host %s" % (route, short(headers["Host"]))
            elif sc == "origin":
                headers["Origin"] = rng.choice(["http://evil.example", "null", "http://127.0.0.1", "", "*"])
                shown = "%s origin %s" % (route, short(headers["Origin"]))
            else:
                method, path = rng.choice(paths)
                obj = {} if method in ("POST", "PUT", "PATCH") else None
                shown = "%s %s" % (method, path)
            status, _ = creq(w, method, path, obj, token=token, headers=headers)
            hostile_status(rec, cls, idx, sc, w, status, shown)
        except Exception as exc:
            rec.add(cls, idx, sc, "console", "error", "setup_" + type(exc).__name__, "", None)
        finally:
            w.close()


def c_console_approval(rec, rng, n, factory):
    cls = "console_approval"
    for _ in range(n):
        idx = rec.next_case(cls)
        sc = rng.choice(["extra_key_spoof", "header_spoof", "self_requester", "digest", "digest", "flow", "flow", "flow"])
        w = World(factory, console=True, ttl_s=float(rng.randint(30, 600)))
        try:
            if sc == "extra_key_spoof":
                p = bridge_propose(w, good_args(), PROPOSER)
                obj = approve_body(p)
                obj[rng.choice(["approver", "who", "user", "person"])] = rng.choice(["person:bob", "person:alice", "employee:agent"])
                hostile_status(rec, cls, idx, sc, w, creq(w, "POST", "/v1/approve", obj)[0], "extra key")
            elif sc == "header_spoof":
                p = bridge_propose(w, good_args(), PROPOSER)
                spoof = {rng.choice(["X-Approver", "X-Forwarded-User", "X-Remote-User", "X-User", "X-Person"]): rng.choice(["person:bob", "employee:agent"])}
                status, _ = creq(w, "POST", "/v1/approve", approve_body(p), headers=spoof)
                who = [e.get("approver") for e in w.bridge.audit if e["event"] == "approved"]
                if status is not None and 200 <= status < 300 and who != ["person:alice"]:
                    rec.add(cls, idx, sc, "console", "passed", "approver_taken_from_a_header:%s" % who, short(spoof), w)
                elif status is None or status >= 500:
                    rec.add(cls, idx, sc, "console", "unclean", "http_%s" % status, short(spoof), w)
                else:
                    rec.add(cls, idx, sc, "console", "refused" if status >= 400 else "refused", "approver_is_the_token_owner" if status < 300 else "http_%d" % status, short(spoof), w)
            elif sc == "self_requester":
                requester = rng.choice(["person:alice", "person:Alice", "PERSON:ALICE", "person:ALICE"])
                p = bridge_propose(w, good_args(), requester)
                hostile_status(rec, cls, idx, sc, w, creq(w, "POST", "/v1/approve", approve_body(p))[0], requester)
            elif sc == "digest":
                p = bridge_propose(w, good_args(), PROPOSER)
                other = bridge_propose(w, {**good_args(), "value": 0.2, "expected_outcome": {**good_args()["expected_outcome"], "lo": 0.15, "hi": 0.25}}, PROPOSER)
                good = p.digest()
                bad = rng.choice(["0" * 64, "", "a" * 63, "a" * 65, good.upper(), good + " ", " " + good, good + "\n", good[:-1] + ("0" if good[-1] != "0" else "1"),
                                  other.digest(), None, 5, [good], {"d": good}, True, good[:32]])
                hostile_status(rec, cls, idx, sc, w, creq(w, "POST", "/v1/approve", {"proposal_id": p.proposal_id, "digest": bad})[0], short(bad))
            else:
                flow = rng.choice(["unapproved_apply", "unknown_ids", "apply_twice", "approve_twice", "approve_after_apply", "expired_apply",
                                   "apply_after_stop", "stop_clear_apply"])
                p = bridge_propose(w, good_args(), PROPOSER)
                if flow == "unapproved_apply":
                    hostile_status(rec, cls, idx, flow, w, creq(w, "POST", "/v1/apply", {"proposal_id": p.proposal_id})[0], flow)
                elif flow == "unknown_ids":
                    pid = rng.choice(["prop_999999", "prop_000000", "prop_1", "prop_00000000000000", "prop_000001x", "../x", "", None, 5, "PROP_000001"])
                    path, obj = rng.choice([("/v1/approve", {"proposal_id": pid, "digest": p.digest()}), ("/v1/apply", {"proposal_id": pid})])
                    hostile_status(rec, cls, idx, flow, w, creq(w, "POST", path, obj)[0], short(pid))
                else:
                    assert creq(w, "POST", "/v1/approve", approve_body(p))[0] == 200
                    if flow == "approve_twice":
                        hostile_status(rec, cls, idx, flow, w, creq(w, "POST", "/v1/approve", approve_body(p))[0], flow)
                    elif flow in ("apply_twice", "approve_after_apply"):
                        assert creq(w, "POST", "/v1/apply", {"proposal_id": p.proposal_id})[0] == 200
                        _reset_sim(w)
                        status = creq(w, "POST", "/v1/apply", {"proposal_id": p.proposal_id})[0] if flow == "apply_twice" else creq(w, "POST", "/v1/approve", approve_body(p))[0]
                        hostile_status(rec, cls, idx, flow, w, status, flow)
                    elif flow == "expired_apply":
                        w.clock.t += 601 + rng.choice([0.001, 1, 3600])
                        hostile_status(rec, cls, idx, flow, w, creq(w, "POST", "/v1/apply", {"proposal_id": p.proposal_id})[0], flow)
                    elif flow == "apply_after_stop":
                        assert creq(w, "POST", "/v1/stop", {})[0] == 200
                        hostile_status(rec, cls, idx, flow, w, creq(w, "POST", "/v1/apply", {"proposal_id": p.proposal_id})[0], flow)
                    else:  # stop, clear-stop, apply: the approval predates the stop and must be refused (a stop invalidates earlier approvals, #1338)
                        assert creq(w, "POST", "/v1/stop", {})[0] == 200 and creq(w, "POST", "/v1/clear-stop", {})[0] == 200
                        hostile_status(rec, cls, idx, flow, w, creq(w, "POST", "/v1/apply", {"proposal_id": p.proposal_id})[0], flow)
        except Exception as exc:
            rec.add(cls, idx, sc, "console", "error", "setup_" + type(exc).__name__, "", None)
        finally:
            w.close()


def c_console_input(rec, rng, n, factory):
    cls = "console_input"
    for _ in range(n):
        idx = rec.next_case(cls)
        sc = rng.choice(["oversize_pad", "deep_nesting", "not_object", "invalid_json", "wrong_type", "bidi_fields", "missing_or_extra", "stop_with_body",
                         "content_type", "length"])
        w = World(factory, console=True)
        try:
            p = bridge_propose(w, good_args(), PROPOSER)
            good = json.dumps(approve_body(p)).encode()
            port = w.console.server_address[1]
            if sc == "oversize_pad":
                size = rng.randint(CONSOLE_MAX_BODY + 1, 70000)
                hostile_status(rec, cls, idx, sc, w, creq(w, "POST", "/v1/approve", raw=good + b" " * (size - len(good)))[0], "size %d" % size)
            elif sc == "deep_nesting":
                d = rng.randint(1200, 2000)
                raw = ('{"proposal_id": ' + "[" * d + "]" * d + ', "digest": "' + p.digest() + '"}').encode()
                hostile_status(rec, cls, idx, sc, w, creq(w, "POST", "/v1/approve", raw=raw)[0], "depth %d" % d)
            elif sc == "not_object":
                raw = rng.choice([b"[1]", b'"x"', b"7", b"null", b"true", b"[]", b'[{"proposal_id": "%s"}]' % p.proposal_id.encode()])
                hostile_status(rec, cls, idx, sc, w, creq(w, "POST", rng.choice(["/v1/approve", "/v1/apply", "/v1/stop"]), raw=raw)[0], short(raw))
            elif sc == "invalid_json":
                raw = rng.choice([good[:-1], good + b"x", b"{", b"", b"\xff\xfe", b"{'a': 1}", b'{"proposal_id": NaN}', good.replace(b'"', b"'")])
                path = "/v1/approve"
                status = creq(w, "POST", path, raw=raw)[0] if raw else creq(w, "POST", path, raw=b"")[0]
                hostile_status(rec, cls, idx, sc, w, status, short(raw))
            elif sc == "wrong_type":
                field = rng.choice(["proposal_id", "digest"])
                value = rng.choice([5, None, [1], {"a": 1}, True, 1.5])
                obj = approve_body(p)
                obj[field] = value
                hostile_status(rec, cls, idx, sc, w, creq(w, "POST", "/v1/approve", obj)[0], "%s=%s" % (field, short(value)))
            elif sc == "bidi_fields":
                ch = chr(rng.choice(BIDI))
                field = rng.choice(["proposal_id", "digest"])
                obj = approve_body(p)
                obj[field] = (obj[field] + ch) if rng.random() < 0.5 else (ch + obj[field])
                hostile_status(rec, cls, idx, sc, w, creq(w, "POST", "/v1/approve", obj)[0], "%s U+%04X" % (field, ord(ch)))
            elif sc == "missing_or_extra":
                obj = approve_body(p)
                kind = rng.choice(["no_digest", "no_id", "extra", "empty"])
                if kind == "no_digest":
                    del obj["digest"]
                elif kind == "no_id":
                    del obj["proposal_id"]
                elif kind == "extra":
                    obj["x"] = 1
                else:
                    obj = {}
                hostile_status(rec, cls, idx, sc, w, creq(w, "POST", "/v1/approve", obj)[0], kind)
            elif sc == "stop_with_body":
                path = rng.choice(["/v1/stop", "/v1/clear-stop"])
                hostile_status(rec, cls, idx, sc, w, creq(w, "POST", path, {"x": 1})[0], path)
            elif sc == "content_type":
                ctype = rng.choice(["text/plain", "", "application/x-www-form-urlencoded", "multipart/form-data", "text/json"])
                hostile_status(rec, cls, idx, sc, w, creq(w, "POST", "/v1/approve", raw=good, ctype=ctype)[0], short(ctype))
            else:
                head = "POST /v1/approve HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nAuthorization: Bearer %s\r\nContent-Type: application/json\r\n" % (port, ALICE_T)
                variant = rng.choice(["abc", "-5", "99999999999999999999", "chunked", "1e3", " ", "+10"])
                if variant == "chunked":
                    req = (head + "Transfer-Encoding: chunked\r\n\r\n").encode() + b"%x\r\n" % len(good) + good + b"\r\n0\r\n\r\n"
                else:
                    req = (head + "Content-Length: %s\r\n\r\n" % variant).encode() + good
                hostile_status(rec, cls, idx, sc, w, raw_status(port, req), "length " + variant)
        except Exception as exc:
            rec.add(cls, idx, sc, "console", "error", "setup_" + type(exc).__name__, "", None)
        finally:
            w.close()


# ---------------------------------------------------------------- hostile sequences with an independent oracle
def fold(s: str) -> str:
    return unicodedata.normalize("NFKC", s).casefold().strip()


def valid_actor(s) -> bool:
    try:
        actor(s, "approver")
        return True
    except Exception:  # noqa: BLE001
        return False


def c_random_sequences(rec, rng, n, factory):
    """Random sequences of propose, approve, apply, tampered apply, forged id, stop, clear-stop and clock jumps. An oracle written from the rules
    (not from the Bridge's code) says for every approve and apply whether it may succeed. A success the oracle forbids is a finding (that includes an approval that
    predates a stop and survives the clear: a stop invalidates earlier approvals, #1338). A refusal the oracle allows is an 'over_refusal' error: the oracle
    and the Bridge disagree and a human has to look. The simulation is restarted and the clock moves on before every apply so that the sequence does
    not hit the rate limit or the 'outcome time is already past' guard by accident."""
    cls = "random_sequences"
    for _ in range(n):
        idx = rec.next_case(cls)
        ttl = float(rng.randint(60, 600))
        w = World(factory, ttl_s=ttl)
        props, trace, stopped, stop_epoch, accepted_applies = {}, [], False, 0, 0

        def judge(op, rules, accepted, shown=""):
            nonlocal_trace = list(trace)
            if accepted and rules:
                rec.add(cls, idx, op, "bridge", "passed", "+".join(rules), shown, w, trace=nonlocal_trace)
            elif accepted is False and not rules:
                rec.add(cls, idx, op, "bridge", "error", "over_refusal", shown, w, trace=nonlocal_trace)
            elif rules:
                rec.add(cls, idx, op, "bridge", "refused", "+".join(rules), shown, w)

        try:
            for _step in range(rng.randint(6, 14)):
                op = rng.choice(["propose"] * 3 + ["approve"] * 4 + ["apply"] * 4 + ["tamper_apply", "forged", "bad_propose", "advance"] + ["stop"] * 2 + ["clear_stop"] * 2 + ["stop_clear"] * 3)
                if _step == 0:
                    op = "propose"
                trace.append(op)
                now = w.clock.t
                live = [pid for pid in props]
                if op == "propose":
                    requester = rng.choice([PROPOSER, "person:alice", "employee:ops"])
                    value = round(rng.uniform(-0.9, 0.9), 3)
                    args = {**good_args(), "value": value, "expected_outcome": {**good_args()["expected_outcome"], "lo": value - 0.05, "hi": value + 0.05}}
                    box = {}
                    v, _c = attempt(lambda: box.update(p=bridge_propose(w, args, requester)))
                    if v == "accepted":
                        p = box["p"]
                        props[p.proposal_id] = {"p": p, "req": requester, "state": "validated", "exp": p.expires_at, "epoch": None}
                elif op == "bad_propose":
                    bad = rng.choice([float("nan"), float("inf"), "0.5", True, None, 1.5, -1.5, 10 ** 400])
                    args = {**good_args(), "value": bad}
                    v, c = attempt(lambda: bridge_propose(w, args, PROPOSER))
                    if v == "accepted":
                        rec.add(cls, idx, op, "bridge", "passed", "bad_value_accepted", short(bad), w, trace=list(trace))
                    else:
                        rec.add(cls, idx, op, "bridge", "refused" if v == "refused" else v, c, short(bad), w)
                elif op == "advance":
                    w.clock.t += rng.choice([1.0, 10.0, ttl * 0.5, ttl * 0.9, ttl * 1.5])
                elif op == "stop":
                    _request_stop(w)
                    stopped, stop_epoch = True, stop_epoch + 1
                elif op == "stop_clear":  # a stop that is cleared at once: every approval given before it must be gone
                    _request_stop(w)
                    stopped, stop_epoch = True, stop_epoch + 1
                    w.bridge.clear_stop()
                    w.adapter.stop_requested = False
                    for q in props.values():
                        if q["state"] == "approved":
                            q["state"] = "validated"
                    stopped = False
                elif op == "clear_stop":
                    w.bridge.clear_stop()
                    w.adapter.stop_requested = False
                    if stopped:  # ending a stop revokes every approval given before it; the proposal itself stays valid
                        for q in props.values():
                            if q["state"] == "approved":
                                q["state"] = "validated"
                    stopped = False
                elif not live:
                    continue
                elif op == "approve":
                    waiting = [q for q in live if props[q]["state"] == "validated"]
                    pid = rng.choice(waiting if (waiting and rng.random() < 0.8) else live)
                    r = props[pid]
                    approver = rng.choice(["person:alice", "person:bob", "person:alice", "person:bob", "person:alice", r["req"], r["req"].upper(), r["req"].swapcase(), "x y"])
                    rules = []
                    if r["state"] != "validated":
                        rules.append("STATE")
                    if not valid_actor(approver):
                        rules.append("APPROVER_ID")
                    elif fold(approver) == fold(r["req"]):
                        rules.append("SELF")
                    if now > r["exp"]:
                        rules.append("EXPIRED")
                    v, c = attempt(lambda: w.bridge.approve(r["p"], approver))
                    judge(op, rules, v == "accepted", "%s by %s" % (pid, short(approver)))
                    if v == "accepted" and not rules:
                        r["state"], r["epoch"] = "approved", stop_epoch
                    elif "EXPIRED" in rules and r["state"] == "validated":
                        r["state"] = "expired"
                elif op in ("apply", "tamper_apply", "forged"):
                    ready = [q for q in live if props[q]["state"] == "approved"]
                    pid = rng.choice(ready if (ready and rng.random() < 0.85) else live)
                    r = props[pid]
                    _reset_sim(w)
                    w.clock.t += 1.0
                    now = w.clock.t
                    target = r["p"]
                    rules = []
                    if op == "tamper_apply":
                        target = dataclasses.replace(r["p"], value=round(0.95 if r["p"].value < 0.9 else -0.95, 3))
                        rules.append("TAMPER")
                    elif op == "forged":
                        target = dataclasses.replace(r["p"], proposal_id="prop_%06d" % rng.randint(500000, 999999))
                        rules.append("UNKNOWN")
                    if r["state"] != "approved":
                        rules.append("STATE")
                    if stopped:
                        rules.append("STOPPED")
                    if now > r["exp"]:
                        rules.append("EXPIRED")
                    before = len(w.adapter.applied)
                    v, c = attempt(lambda: w.bridge.apply(target))
                    judge(op, rules, v == "accepted", pid)
                    if v == "accepted":
                        accepted_applies += 1
                        r["state"] = "applied"
                    elif "EXPIRED" in rules and r["state"] == "approved" and op == "apply":
                        r["state"] = "expired"
                    if len(w.adapter.applied) != before + (1 if v == "accepted" else 0):
                        rec.add(cls, idx, op, "bridge", "passed", "device_command_count_differs_from_accepted_applies", pid, w, trace=list(trace))
        except Exception as exc:
            rec.add(cls, idx, "sequence", "bridge", "error", "setup_" + type(exc).__name__, short(exc), None, trace=list(trace))
        finally:
            w.close()


# ---------------------------------------------------------------- known gaps: probed and named, never counted as new findings
def unread_at_close_probe(factory):
    """MCP server, body of four times the linger limit: how many request bytes are still unread when the server closes (Linux and macOS only)."""
    if fcntl is None:
        return None
    w = World(factory, server=True)
    sent, unread = threading.Event(), []
    try:
        orig = w.server.shutdown_request

        def hooked(request):
            sent.wait(4.0)  # the client gives up sending after 2 s (below), so this wait is short
            time.sleep(0.05)
            buf = array.array("i", [0])
            try:
                fcntl.ioctl(request.fileno(), termios.FIONREAD, buf, True)
                unread.append(buf[0])
            except OSError:
                unread.append(-1)
            orig(request)
        w.server.shutdown_request = hooked
        port, size = w.server.server_address[1], 4 * LINGER_MAX_BYTES
        c = socket.create_connection(("127.0.0.1", port), timeout=20)
        c.settimeout(2.0)  # the server stops reading at its limit, so a send of four times that blocks: do not wait for it
        try:
            c.sendall(("POST /mcp HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n" % (port, size)).encode())
            chunk, left = b" " * 65536, size
            try:
                while left > 0:
                    k = min(left, len(chunk))
                    c.sendall(chunk[:k])
                    left -= k
            except OSError:
                pass
            finally:
                sent.set()
            end = time.monotonic() + 8.0
            while not unread and time.monotonic() < end:
                time.sleep(0.01)
        finally:
            c.close()
    finally:
        w.close()
    return unread[0] if unread else None


def known_gap_probes(factory, rec, include_slow):
    out = {k: {"description": d, "status": "not probed", "cases": len(rec.known.get(k, []))} for k, d in KNOWN_GAPS.items()}
    # #1339: a stop lands at the entry of the adapter call
    w = World(factory)
    try:
        p = _approved(w)
        w.adapter.trigger = ("apply_entry", 1)
        w.adapter.on_trigger = lambda: _request_stop(w)
        attempt(lambda: w.bridge.apply(p))
        out["#1339"]["status"] = "open (reproduced)" if w.adapter.late_applies else "not reproduced (closed? remove the marker)"
    except Exception as exc:  # noqa: BLE001
        out["#1339"]["status"] = "not probed (%s)" % type(exc).__name__
    finally:
        w.close()
    # the 1 MiB linger limit
    unread = unread_at_close_probe(factory)
    if unread is not None:
        out["linger-limit"]["status"] = "open (reproduced)" if unread > 0 else "not reproduced (closed? remove the marker)"
    else:
        out["linger-limit"]["status"] = "not probed (needs fcntl: Linux or macOS)"
    # #1367: only with --include-slow (about 10 s)
    if include_slow:
        w = World(factory, console=True)
        try:
            port = w.console.server_address[1]
            stalled = socket.create_connection(("127.0.0.1", port), timeout=30)
            try:
                stalled.sendall(("POST /v1/apply HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nAuthorization: Bearer %s\r\nContent-Type: application/json\r\nContent-Length: 1000\r\n\r\n" % (port, "c" * 24)).encode())
                time.sleep(0.1)
                started = time.monotonic()
                status, _ = creq(w, "POST", "/v1/stop", {})
                waited = time.monotonic() - started
            finally:
                stalled.close()
            out["#1367"]["status"] = "open (reproduced)" if waited > 5.0 else "not reproduced (closed? remove the marker)"
        finally:
            w.close()
    return out


RUNNERS = {"bad_values": c_bad_values, "wrong_targets": c_wrong_targets, "stale_or_reused_approvals": c_stale_or_reused,
           "self_approval": c_self_approval, "oversize_or_nested": c_oversize_nested, "bidi_and_control": c_bidi,
           "replay_after_expiry": c_replay, "stop_races": c_stop_races, "forbidden_access": c_forbidden,
           "console_auth": c_console_auth, "console_approval": c_console_approval, "console_input": c_console_input,
           "random_sequences": c_random_sequences}


# ---------------------------------------------------------------- controls
def _ok(fn) -> bool:
    """A control that raises is a failed control (exit code 2), never a crash of the harness."""
    try:
        return bool(fn())
    except Exception:  # noqa: BLE001
        return False


def controls(factory) -> dict:
    """The harness is only trusted if legitimate use works on the same wiring (otherwise 'all refused' would mean nothing)."""
    out = {}
    w = World(factory)
    out["propose_via_bridge"] = attempt(lambda: bridge_propose(w, good_args()))[0] == "accepted"
    w = World(factory)
    out["propose_via_tools"] = _ok(lambda: w.tools.call("physical_propose_action", good_args()).get("ok") is True)
    w = World(factory, server=True)
    try:
        out["propose_via_mcp"] = _ok(lambda: mcp_verdict(*w.post(rpc_call("physical_propose_action", good_args())))[0] == "accepted")
    finally:
        w.close()
    w = World(factory)
    try:
        p = _approved(w)
        r = w.bridge.apply(p)
        out["full_cycle_verified"] = r.state == "verified" and r.outcome_ok
    except Exception:  # noqa: BLE001
        out["full_cycle_verified"] = False
    w = World(factory)
    try:  # a stop cancels earlier approvals; a fresh approval after the clear must work again (so the refusal above is not over-refusal)
        p = _approved(w)
        _request_stop(w)
        w.bridge.clear_stop()
        w.adapter.stop_requested = False
        try:
            w.bridge.approve(p, APPROVER)
        except PhysicalError:  # still approved from before the stop: the classes below must report that; this control only checks for over-refusal
            pass
        r = w.bridge.apply(p)
        out["fresh_approval_after_clear_applies"] = r.state == "verified" and r.outcome_ok
    except Exception:  # noqa: BLE001
        out["fresh_approval_after_clear_applies"] = False
    w = World(factory, console=True)
    try:  # the console's whole legitimate cycle: approve, apply, stop, clear-stop, each by a named person's token
        p = bridge_propose(w, good_args(), PROPOSER)
        ok = creq(w, "POST", "/v1/approve", approve_body(p))[0] == 200
        status, body = creq(w, "POST", "/v1/apply", {"proposal_id": p.proposal_id})
        ok = ok and status == 200 and body.get("state") == "verified"
        ok = ok and creq(w, "POST", "/v1/stop", {})[0] == 200 and creq(w, "POST", "/v1/clear-stop", {})[0] == 200
        out["console_full_cycle"] = bool(ok) and [e.get("approver") for e in w.bridge.audit if e["event"] == "approved"] == ["person:alice"]
    except Exception:  # noqa: BLE001
        out["console_full_cycle"] = False
    finally:
        w.close()
    w = World(factory)
    a = good_args()
    a["reason"] = "\u0bae\u0bc7\u0b9a\u0bc8 \u0b9a\u0bb0\u0bbf\u0baa\u0bbe\u0bb0\u0bcd\u0baa\u0bcd\u0baa\u0bc1 \u0939\u093f\u0928\u094d\u0926\u0940 \u200d"
    out["tamil_hindi_reason_accepted"] = attempt(lambda: bridge_propose(w, a))[0] == "accepted"
    w = World(factory, server=True)
    try:
        s, parsed = w.post(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode())
        out["four_tools_listed"] = s == 200 and len(parsed["result"]["tools"]) == 4
    finally:
        w.close()
    return out


def python_dependent_observations(factory) -> list:
    """What the standard-library HTTP layer does, which can differ between Python versions (so it is kept out of the report hash)."""
    try:
        w = World(factory, server=True)
        try:
            status, _ = w.post(rpc_call("physical_get_observation", {}), path="//mcp")
            return [{"name": "double_slash_path_served_as_mcp", "outcome": "served" if status == 200 else "refused_http_%s" % status,
                     "note": "Python's http.server may rewrite //mcp to /mcp; the request then passes every normal check"}]
        finally:
            w.close()
    except Exception as exc:  # noqa: BLE001
        return [{"name": "stdlib_probe_not_available", "outcome": type(exc).__name__, "note": ""}]


def load_invariants():
    try:
        return json.loads(INVARIANTS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- run and report
VERDICTS = ("refused", "unclean", "passed", "known", "inconclusive", "error")


def run(seed: int = 1, per_class: int = 40, factory=default_factory, only=None, include_slow: bool = False) -> dict:
    rec = Recorder(seed)
    ctl = controls(factory)
    for ci, cls in enumerate(CLASSES):
        if only and cls not in only:
            continue
        RUNNERS[cls](rec, random.Random(seed * 1000 + ci), per_class, factory)
    classes = {}
    inv = load_invariants()
    for cls, scenario, layer, verdict, code in rec.rows:
        c = classes.setdefault(cls, dict({"attempts": 0, "by_layer": {}, "by_scenario": {}, "codes": {}, "invariants": (inv or {}).get("classes", {}).get(cls)}, **{v: 0 for v in VERDICTS}))
        c["attempts"] += 1
        c[verdict] += 1
        c["by_layer"][layer] = c["by_layer"].get(layer, 0) + 1
        s = c["by_scenario"].setdefault(scenario, {v: 0 for v in VERDICTS})
        s[verdict] += 1
        c["codes"][code] = c["codes"].get(code, 0) + 1
    body = {"schema": SCHEMA, "seed": seed, "per_class": per_class, "package_version": __version__, "controls": ctl,
            "controls_ok": all(ctl.values()), "classes": {k: classes[k] for k in sorted(classes)},
            "totals": {k: sum(c[k] for c in classes.values()) for k in ("attempts",) + VERDICTS},
            "findings": rec.findings, "known_gaps": known_gap_probes(factory, rec, include_slow),
            "invariants_source": (inv or {}).get("source", "missing: invariants.json has not been written from docs/safety-invariants.md"),
            "invariants_not_covered": sorted(set((inv or {}).get("invariants", [])) - {i for ids in (inv or {}).get("classes", {}).values() for i in ids}, key=lambda s: int(s[1:]))}
    body["report_sha256"] = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    body["environment"] = {"python": platform.python_version(), "platform": platform.system(),
                           "python_dependent_observations": python_dependent_observations(factory)}  # not part of the hash
    return body


def table(rep: dict) -> str:
    fmt = "%-26s %8s %8s %8s %7s %6s %8s %6s"
    lines = [fmt % ("class", "attempts", "refused", "unclean", "PASSED", "known", "inconcl.", "error")]
    for k, c in rep["classes"].items():
        lines.append(fmt.replace("s", "s", 1) % (k, c["attempts"], c["refused"], c["unclean"], c["passed"], c["known"], c["inconclusive"], c["error"]))
    t = rep["totals"]
    lines.append(fmt % ("TOTAL", t["attempts"], t["refused"], t["unclean"], t["passed"], t["known"], t["inconclusive"], t["error"]))
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--per-class", type=int, default=40)
    ap.add_argument("--only", action="append", choices=CLASSES)
    ap.add_argument("--json")
    ap.add_argument("--include-slow", action="store_true", help="also probe #1367 (about 10 s)")
    a = ap.parse_args(argv)
    rep = run(a.seed, a.per_class, only=a.only, include_slow=a.include_slow)
    print("ragleap-physical %s | python %s | seed %d | per class %d" % (rep["package_version"], rep["environment"]["python"], a.seed, a.per_class))
    print("controls:", "OK" if rep["controls_ok"] else "FAILED " + json.dumps(rep["controls"]))
    print(table(rep))
    for f in rep["findings"]:
        print("FINDING seed=%d class=%s case=%d scenario=%s layer=%s %s: %s (%s)%s" % (f["seed"], f["class"], f["case"], f["scenario"], f["layer"], f["verdict"], f["detail"], f["input"],
                                                                                  (" trace=" + ",".join(f["trace"])) if f.get("trace") else ""))
    for k, g in rep["known_gaps"].items():
        print("KNOWN %-12s %-45s cases seen: %d  (%s)" % (k, g["status"], g["cases"], g["description"][:70]))
    print("report_sha256", rep["report_sha256"])
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, indent=1, sort_keys=True)
    if not rep["controls_ok"]:
        return 2
    return 1 if (rep["totals"]["passed"] or rep["totals"]["unclean"] or rep["totals"]["error"]) else 0


if __name__ == "__main__":
    sys.exit(main())
