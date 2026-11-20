"""ragleap_flows.flow - a flow is plain JSON data, checked once, strictly, before anything runs.

A flow file holds no code: steps name a tool, an approval checkpoint or a branch, and values are moved
between steps with a tiny path syntax ($input.x, $step_id.field.0). There is no eval, no attribute access
and no string interpolation. Unknown keys, wrong types, unknown targets, cycles, unreachable steps and
oversize files are all refused with FlowError, so a bad file never reaches a run.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

MAX_STEPS = 100
MAX_FLOW_BYTES = 256 * 1024
MAX_INPUT_BYTES = 64 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
MAX_STATE_BYTES = 1024 * 1024
DEADLINE_SECONDS = 300.0
MAX_PROMPT_CHARS = 500
MAX_DEPTH = 8

_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
_TOOL = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
_SEGMENT = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
OPS = ("eq", "ne", "lt", "le", "gt", "ge", "in", "exists", "truthy")
_NO_VALUE = ("exists", "truthy")
_RESERVED = ("input",)


class FlowError(ValueError):
    """A flow file or a FlowRunner was configured wrongly. Raised while loading, never from run()."""


class Missing(Exception):
    """A path points at a value that does not exist (internal; the runner turns it into a failed run)."""


@dataclass(frozen=True)
class Flow:
    """A validated flow. Build it with from_json() or from_dict(); do not construct it by hand."""

    name: Optional[str]
    start: str
    steps: Dict[str, Dict[str, Any]]
    output: Any
    digest: str
    has_tool_steps: bool


def _fail(msg: str) -> "FlowError":
    return FlowError(msg)


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _no_dups(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise _fail("duplicate key in flow file")
        out[k] = v
    return out


def _no_constant(name: str) -> Any:
    raise _fail("NaN and Infinity are not allowed in a flow file")


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def parse_path(path: Any) -> Tuple[str, List[str]]:
    """'$input.a.0' -> ('input', ['a', '0']). Raises FlowError for anything else."""
    if not isinstance(path, str) or not path.startswith("$") or path.startswith("$$"):
        raise _fail("a path must be a string starting with a single $")
    parts = path[1:].split(".")
    if not parts or any(not _SEGMENT.match(p) for p in parts) or len(parts) > MAX_DEPTH:
        raise _fail("invalid path")
    return parts[0], parts[1:]


def resolve_path(path: str, values: Dict[str, Any]) -> Any:
    """Follow a path through dicts (by key) and lists (by integer position). Never touches attributes."""
    head, rest = parse_path(path)
    if head not in values:
        raise Missing()
    cur = values[head]
    for seg in rest:
        if isinstance(cur, dict):
            if seg not in cur:
                raise Missing()
            cur = cur[seg]
        elif isinstance(cur, list):
            if not seg.isdigit() or (len(seg) > 1 and seg[0] == "0") or int(seg) >= len(cur):
                raise Missing()
            cur = cur[int(seg)]
        else:
            raise Missing()
    return cur


def resolve_template(template: Any, values: Dict[str, Any]) -> Any:
    """Copy a template, replacing every string that is exactly a path by the value it points at.
    A string starting with $$ becomes a literal string starting with one $. No interpolation."""
    if isinstance(template, str):
        if template.startswith("$$"):
            return template[1:]
        if template.startswith("$"):
            return resolve_path(template, values)
        return template
    if isinstance(template, list):
        return [resolve_template(t, values) for t in template]
    if isinstance(template, dict):
        return {k: resolve_template(v, values) for k, v in template.items()}
    return template


def _check_json_value(v: Any, depth: int = 0) -> None:
    if depth > MAX_DEPTH:
        raise _fail("a value is nested too deeply")
    if v is None or isinstance(v, (bool, str)) or _is_int(v):
        return
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            raise _fail("NaN and Infinity are not allowed")
        return
    if isinstance(v, list):
        for x in v:
            _check_json_value(x, depth + 1)
        return
    if isinstance(v, dict):
        for k, x in v.items():
            if not isinstance(k, str):
                raise _fail("object keys must be strings")
            _check_json_value(x, depth + 1)
        return
    raise _fail("a value is not plain JSON")


def _paths_in(template: Any, out: List[str]) -> None:
    if isinstance(template, str):
        if template.startswith("$") and not template.startswith("$$"):
            out.append(template)
    elif isinstance(template, list):
        for t in template:
            _paths_in(t, out)
    elif isinstance(template, dict):
        for t in template.values():
            _paths_in(t, out)


def _only(obj: Dict[str, Any], required: Tuple[str, ...], optional: Tuple[str, ...], what: str) -> None:
    for k in required:
        if k not in obj:
            raise _fail("%s needs %r" % (what, k))
    for k in obj:
        if k not in required and k not in optional:
            raise _fail("%s has an unknown key %r" % (what, k[:40]))


def _target(v: Any, ids: Any, what: str) -> Optional[str]:
    if v is None:
        return None
    if not isinstance(v, str) or v not in ids:
        raise _fail("%s points at a step that does not exist" % what)
    return v


def _check_cond(c: Any, depth: int, refs: List[str]) -> None:
    if depth > 4 or not isinstance(c, dict):
        raise _fail("a condition must be an object, nested at most 4 deep")
    if "all" in c or "any" in c:
        _only(c, (), ("all", "any"), "a condition")
        if len(c) != 1:
            raise _fail("a condition uses one of all or any")
        key = "all" if "all" in c else "any"
        items = c[key]
        if not isinstance(items, list) or not 1 <= len(items) <= 10:
            raise _fail("all and any hold 1 to 10 conditions")
        for x in items:
            _check_cond(x, depth + 1, refs)
        return
    _only(c, ("path", "op"), ("value",), "a condition")
    parse_path(c["path"])
    refs.append(c["path"])
    if c["op"] not in OPS:
        raise _fail("unknown operator")
    if c["op"] in _NO_VALUE:
        if "value" in c:
            raise _fail("this operator takes no value")
        return
    if "value" not in c:
        raise _fail("this operator needs a value")
    _check_json_value(c["value"])
    if c["op"] == "in" and not isinstance(c["value"], list):
        raise _fail("'in' needs a list")


def _validate(d: Any) -> Flow:
    if not isinstance(d, dict):
        raise _fail("a flow must be a JSON object")
    _only(d, ("version", "steps"), ("name", "start", "output"), "a flow")
    if not _is_int(d["version"]) or d["version"] != 1:
        raise _fail("unsupported flow version")
    name = d.get("name")
    if name is not None and (not isinstance(name, str) or not _NAME.match(name)):
        raise _fail("invalid flow name")
    raw = d["steps"]
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_STEPS:
        raise _fail("a flow has 1 to %d steps" % MAX_STEPS)
    steps: Dict[str, Dict[str, Any]] = {}
    for s in raw:
        if not isinstance(s, dict) or not isinstance(s.get("id"), str) or not _ID.match(s["id"]):
            raise _fail("every step needs an id of 1-64 of A-Z a-z 0-9 _ -")
        if s["id"] in steps or s["id"] in _RESERVED:
            raise _fail("duplicate or reserved step id")
        steps[s["id"]] = s
    ids = set(steps)
    refs: Dict[str, List[str]] = {}
    edges: Dict[str, List[str]] = {}
    has_tool = False
    for sid, s in steps.items():
        typ = s.get("type")
        r: List[str] = []
        if typ == "tool":
            _only(s, ("id", "type", "tool"), ("arguments", "next", "outbound", "external"), "a tool step")
            if not isinstance(s["tool"], str) or not _TOOL.match(s["tool"]):
                raise _fail("invalid tool name")
            args = s.get("arguments", {})
            if not isinstance(args, dict):
                raise _fail("tool arguments must be an object")
            _check_json_value(args)
            _paths_in(args, r)
            for p in r:
                parse_path(p)
            for flag in ("outbound", "external"):
                if flag in s and not isinstance(s[flag], bool):
                    raise _fail("%s must be true or false" % flag)
            nxt = [_target(s.get("next"), ids, "next")]
            has_tool = True
        elif typ == "approval":
            _only(s, ("id", "type", "prompt"), ("show", "next"), "an approval step")
            if not isinstance(s["prompt"], str) or not 1 <= len(s["prompt"]) <= MAX_PROMPT_CHARS:
                raise _fail("an approval prompt is 1 to %d characters" % MAX_PROMPT_CHARS)
            show = s.get("show", {})
            if not isinstance(show, dict):
                raise _fail("show must be an object")
            _check_json_value(show)
            _paths_in(show, r)
            for p in r:
                parse_path(p)
            nxt = [_target(s.get("next"), ids, "next")]
        elif typ == "branch":
            _only(s, ("id", "type", "cases"), ("else",), "a branch step")
            cases = s["cases"]
            if not isinstance(cases, list) or not 1 <= len(cases) <= 20:
                raise _fail("a branch has 1 to 20 cases")
            nxt = []
            for c in cases:
                if not isinstance(c, dict):
                    raise _fail("a case must be an object")
                _only(c, ("when", "goto"), (), "a case")
                _check_cond(c["when"], 0, r)
                nxt.append(_target(c["goto"], ids, "goto"))
            nxt.append(_target(s.get("else"), ids, "else"))
        else:
            raise _fail("unknown step type")
        for p in r:
            head = parse_path(p)[0]
            if head != "input" and head not in ids:
                raise _fail("a path names a step that does not exist")
        refs[sid] = r
        edges[sid] = [t for t in nxt if t is not None]
    start = d.get("start", raw[0]["id"])
    if not isinstance(start, str) or start not in ids:
        raise _fail("start names a step that does not exist")
    state: Dict[str, int] = {}

    def visit(n: str) -> None:
        state[n] = 1
        for t in edges[n]:
            if state.get(t) == 1:
                raise _fail("a flow must not contain a cycle")
            if t not in state:
                visit(t)
        state[n] = 2

    visit(start)
    if set(state) != ids:
        raise _fail("every step must be reachable from the start")
    # a path may only read a step that always runs before it (on every route to it)
    preds: Dict[str, List[str]] = {i: [] for i in ids}
    for n, ts in edges.items():
        for t in ts:
            preds[t].append(n)
    always: Dict[str, Optional[set]] = {}

    def must_before(n: str) -> set:
        if n in always and always[n] is not None:
            return always[n]  # type: ignore[return-value]
        if n == start:
            res: set = set()
        else:
            sets = [must_before(p) | {p} for p in preds[n]]
            res = set.intersection(*sets) if sets else set()
        always[n] = res
        return res

    for sid, plist in refs.items():
        for p in plist:
            head = parse_path(p)[0]
            if head != "input" and head not in must_before(sid):
                raise _fail("a path reads a step that does not always run before it")
    out = d.get("output")
    if out is not None:
        _check_json_value(out)
        outp: List[str] = []
        _paths_in(out, outp)
        for p in outp:
            head = parse_path(p)[0]
            if head != "input" and head not in ids:
                raise _fail("the output names a step that does not exist")
    return Flow(name=name, start=start, steps=steps, output=out,
                digest=hashlib.sha256(canonical(d).encode("ascii")).hexdigest(), has_tool_steps=has_tool)


def from_dict(d: Any) -> Flow:
    try:
        if len(canonical(d).encode("ascii")) > MAX_FLOW_BYTES:
            raise _fail("the flow file is too large (max %d bytes)" % MAX_FLOW_BYTES)
        return _validate(d)
    except FlowError:
        raise
    except (TypeError, ValueError, RecursionError):
        raise _fail("the flow is not plain JSON") from None


def from_json(text: Any) -> Flow:
    if isinstance(text, (bytes, bytearray)):
        if len(text) > MAX_FLOW_BYTES:
            raise _fail("the flow file is too large (max %d bytes)" % MAX_FLOW_BYTES)
        try:
            text = bytes(text).decode("utf-8")
        except UnicodeDecodeError:
            raise _fail("the flow file is not UTF-8") from None
    if not isinstance(text, str):
        raise _fail("a flow file is text or bytes")
    if len(text.encode("utf-8")) > MAX_FLOW_BYTES:
        raise _fail("the flow file is too large (max %d bytes)" % MAX_FLOW_BYTES)
    try:
        d = json.loads(text, object_pairs_hook=_no_dups, parse_constant=_no_constant)
    except FlowError:
        raise
    except (ValueError, RecursionError):
        raise _fail("the flow file is not valid JSON") from None
    return from_dict(d)


def _same(a: Any, b: Any) -> bool:
    """Equality where a bool only equals a bool (True is not 1)."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    return a == b


def eval_cond(c: Dict[str, Any], values: Dict[str, Any]) -> bool:
    """Evaluate a validated condition. A path that does not exist makes every operator False, including
    'ne' (a missing value never satisfies a comparison)."""
    if "all" in c:
        return all(eval_cond(x, values) for x in c["all"])
    if "any" in c:
        return any(eval_cond(x, values) for x in c["any"])
    op = c["op"]
    try:
        v = resolve_path(c["path"], values)
    except Missing:
        return False
    if op == "exists":
        return True
    if op == "truthy":
        return bool(v)
    ref = c["value"]
    if op == "in":
        return any(_same(v, x) for x in ref)
    if op == "eq":
        return _same(v, ref)
    if op == "ne":
        return not _same(v, ref)
    if isinstance(v, bool) or isinstance(ref, bool):
        return False
    both_num = isinstance(v, (int, float)) and isinstance(ref, (int, float))
    both_str = isinstance(v, str) and isinstance(ref, str)
    if not (both_num or both_str):
        return False
    return {"lt": v < ref, "le": v <= ref, "gt": v > ref, "ge": v >= ref}[op]
