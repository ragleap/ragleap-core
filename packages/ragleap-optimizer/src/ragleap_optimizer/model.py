"""Data model for ragleap-optimizer: the report, the result, and the errors."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Tuple

Request = Dict[str, Any]
Counter = Callable[[str], int]


class OptimizerError(Exception):
    """Base class. Messages never contain request text."""


class InvalidRequestError(OptimizerError, ValueError):
    """The request or an argument is invalid."""


def estimate_tokens(text: str) -> int:
    """Rough, conservative count: one token per 3 UTF-8 bytes, at least 1. An estimate, not any model's
    tokenizer: it over-counts English (about 4 bytes per token). Pass your own counter where you need one."""
    return max(1, math.ceil(len(text.encode("utf-8")) / 3))


def content_text(content: Any) -> str:
    """The text of a message's content: a string, or a list of parts (text parts count by their text,
    any other part by its JSON form)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                out.append(part["text"])
            else:
                out.append(json.dumps(part, sort_keys=True, default=str))
        return "\n".join(out)
    return json.dumps(content, sort_keys=True, default=str)


def count_request(request: Request, counter: Counter = estimate_tokens) -> int:
    """Tokens of everything in the request that is sent as input: messages, tools, and the system field."""
    total = 0
    for m in request.get("messages", []):
        total += counter(content_text(m.get("content", ""))) + counter(str(m.get("role", "")))
    if "tools" in request:
        total += counter(json.dumps(request["tools"], sort_keys=True, default=str))
    if isinstance(request.get("system"), str):
        total += counter(request["system"])
    return total


@dataclass(frozen=True)
class StepReport:
    name: str
    tokens_before: int
    tokens_after: int
    lossy: bool          # True only if this step actually removed or replaced information in this run
    changed: bool        # True if the request differs from before
    skipped: str = ""    # why the step's result was discarded, if it was


@dataclass(frozen=True)
class Report:
    tokens_before: int
    tokens_after: int
    steps: Tuple[StepReport, ...] = ()

    @property
    def lossy(self) -> bool:
        return any(s.lossy for s in self.steps)

    @property
    def tokens_saved(self) -> int:
        return self.tokens_before - self.tokens_after


@dataclass(frozen=True)
class Optimized:
    request: Request
    report: Report
