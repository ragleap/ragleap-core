"""Steps for the Optimizer. A step takes a request and returns (new_request, lossy). It never mutates its
input. `lossy` is True only if this run removed or replaced information."""
from __future__ import annotations

import copy
import json
import re
from typing import Any, Callable, Dict, List, Tuple

from .model import Counter, InvalidRequestError, Request, content_text, estimate_tokens

Step = Callable[[Request, Counter], "Tuple[Request, bool]"]
_BLANKS = re.compile(r"\n{3,}")


def _named(name: str, fn: Callable) -> Callable:
    fn.step_name = name  # type: ignore[attr-defined]
    return fn


def _pos_int(name: str, v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < 1:
        raise InvalidRequestError(f"{name} must be a positive int")
    return v


def dedupe_context(min_chars: int = 200) -> Step:
    """Remove a message that is an exact copy (same role and content) of a LATER message, when it is at
    least `min_chars` long. System messages and short messages are never touched, so a repeated 'yes' stays.
    The last copy is kept, so the most recent position of the content is preserved. Nothing is lost: the
    removed text is still in the request."""
    _pos_int("min_chars", min_chars)

    def run(request: Request, counter: Counter) -> "Tuple[Request, bool]":
        msgs = request.get("messages", [])
        keep = [True] * len(msgs)
        seen = set()
        for i in range(len(msgs) - 1, -1, -1):
            m = msgs[i]
            text = content_text(m.get("content", ""))
            if m.get("role") == "system" or len(text) < min_chars:
                continue
            key = (m.get("role"), json.dumps(m.get("content"), sort_keys=True, default=str))
            if key in seen:
                keep[i] = False
            seen.add(key)
        out = copy.deepcopy(request)
        out["messages"] = [m for m, k in zip(copy.deepcopy(msgs), keep) if k]
        return out, False
    return _named("dedupe_context", run)


def trim_tool_output(max_tokens: int, roles: Tuple[str, ...] = ("tool",), keep_tail_fraction: float = 0.25) -> Step:
    """Shorten any message of the given roles whose text is over `max_tokens`: keep the start and the end and
    put an explicit marker in between. THIS LOSES INFORMATION (the middle), so a step that trims reports
    lossy=True. A message is only trimmed if that really saves tokens, and only string content is trimmed."""
    _pos_int("max_tokens", max_tokens)
    if not isinstance(keep_tail_fraction, (int, float)) or isinstance(keep_tail_fraction, bool) or not 0 <= keep_tail_fraction < 1:
        raise InvalidRequestError("keep_tail_fraction must be at least 0 and below 1")

    def run(request: Request, counter: Counter) -> "Tuple[Request, bool]":
        out = copy.deepcopy(request)
        lossy = False
        for m in out.get("messages", []):
            c = m.get("content")
            if m.get("role") not in roles or not isinstance(c, str) or counter(c) <= max_tokens:
                continue
            ratio = max_tokens / counter(c)
            budget = max(1, int(len(c) * ratio * 0.9))          # characters to keep, a little under the cap
            tail = int(budget * keep_tail_fraction)
            head = budget - tail
            omitted = len(c) - head - tail
            marker = f"\n[... {omitted} characters omitted by ragleap-optimizer ...]\n"
            new = c[:head] + marker + (c[len(c) - tail:] if tail else "")
            if counter(new) < counter(c):
                m["content"] = new
                lossy = True
        return out, lossy
    return _named("trim_tool_output", run)


def minify_whitespace() -> Step:
    """Collapse three or more newlines in a row into two, and write a message whose content is entirely JSON
    in its compact form. The words and the data stay the same; only spacing changes."""
    def run(request: Request, counter: Counter) -> "Tuple[Request, bool]":
        out = copy.deepcopy(request)
        for m in out.get("messages", []):
            c = m.get("content")
            if not isinstance(c, str):
                continue
            stripped = c.strip()
            if stripped[:1] in "{[":
                try:
                    c = json.dumps(json.loads(stripped), separators=(",", ":"), ensure_ascii=False)
                except ValueError:
                    pass
            m["content"] = _BLANKS.sub("\n\n", c)
        return out, False
    return _named("minify_whitespace", run)


def stable_prefix(sort_tools: bool = False) -> Step:
    """Make the start of the request byte-identical from call to call, so a provider's prompt cache can
    recognise it: system messages first (keeping their order), then the others in their original order;
    JSON keys of tools sorted. `sort_tools=True` also orders the tools by name, which a model could in
    principle react to, so it is off by default."""
    def canon(x: Any) -> Any:
        return json.loads(json.dumps(x, sort_keys=True, default=str))

    def run(request: Request, counter: Counter) -> "Tuple[Request, bool]":
        out = copy.deepcopy(request)
        msgs = out.get("messages", [])
        out["messages"] = [m for m in msgs if m.get("role") == "system"] + [m for m in msgs if m.get("role") != "system"]
        if "tools" in out and isinstance(out["tools"], list):
            tools = canon(out["tools"])
            if sort_tools:
                tools = sorted(tools, key=lambda t: json.dumps(t.get("function", t).get("name", "") if isinstance(t, dict) else "", sort_keys=True))
            out["tools"] = tools
        return out, False
    return _named("stable_prefix", run)
