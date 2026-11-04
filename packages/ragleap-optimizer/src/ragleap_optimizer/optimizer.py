"""Optimizer: runs steps over a request and reports exactly what each one did."""
from __future__ import annotations

import copy
import json
from typing import Any, Optional, Sequence

from ragleap_security import AuditLog, Scope, check_size, require_scope

from .model import (Counter, InvalidRequestError, Optimized, Report, Request, StepReport, count_request,
                    estimate_tokens)
from .steps import Step


def _validate_request(request: Any, max_bytes: int, max_messages: int) -> None:
    if not isinstance(request, dict):
        raise InvalidRequestError("request must be a dict")
    msgs = request.get("messages", [])
    if not isinstance(msgs, list) or len(msgs) > max_messages:
        raise InvalidRequestError(f"messages must be a list of at most {max_messages}")
    for m in msgs:
        if not isinstance(m, dict) or not isinstance(m.get("role"), str):
            raise InvalidRequestError("every message must be a dict with a string role")
    try:
        text = json.dumps(request, sort_keys=True)
    except (TypeError, ValueError):
        raise InvalidRequestError("request must be JSON serializable") from None
    check_size(text, max_bytes, "request")


class Optimizer:
    """Steps run in order. A step whose result has MORE tokens than its input is discarded and reported as
    skipped, so a pipeline never makes a request longer. `report.lossy` is True only if a step actually
    removed or replaced information in this run."""

    def __init__(self, steps: Sequence[Step], *, counter: Counter = estimate_tokens, audit: Optional[AuditLog] = None,
                 max_request_bytes: int = 4 * 1024 * 1024, max_messages: int = 10_000):
        steps = list(steps)
        if len(steps) > 50 or not all(callable(s) and isinstance(getattr(s, "step_name", None), str) for s in steps):
            raise InvalidRequestError("steps must be up to 50 steps made by ragleap_optimizer.steps")
        if not callable(counter):
            raise InvalidRequestError("counter must be callable")
        for name, v in (("max_request_bytes", max_request_bytes), ("max_messages", max_messages)):
            if isinstance(v, bool) or not isinstance(v, int) or v < 1:
                raise InvalidRequestError(f"{name} must be a positive int")
        def checked(text: str) -> int:
            n = counter(text)
            if isinstance(n, bool) or not isinstance(n, int) or n < 0:
                raise InvalidRequestError("counter must return a non-negative int")
            return n
        self._steps = steps
        self._counter = checked
        self._audit = audit
        self._max_bytes = max_request_bytes
        self._max_messages = max_messages

    def _count(self, request: Request) -> int:
        return count_request(request, self._counter)

    def run(self, request: Request, scope: Optional[Scope] = None) -> Optimized:
        _validate_request(request, self._max_bytes, self._max_messages)
        if self._audit is not None:
            scope = require_scope(scope)  # an audit record needs a scope
        current = copy.deepcopy(request)
        before = self._count(current)
        reports = []
        for step in self._steps:
            t0 = self._count(current)
            new, lossy = step(current, self._counter)
            t1 = self._count(new)
            if t1 > t0:
                reports.append(StepReport(step.step_name, t0, t0, False, False, "result had more tokens"))
                continue
            changed = new != current
            reports.append(StepReport(step.step_name, t0, t1, bool(lossy) and changed, changed))
            current = new
        report = Report(before, self._count(current), tuple(reports))
        if self._audit is not None:
            self._audit.append("optimizer.run", {"tokens_before": report.tokens_before, "tokens_after": report.tokens_after,
                                                 "steps": [s.name for s in reports], "lossy": report.lossy}, scope)
        return Optimized(current, report)
