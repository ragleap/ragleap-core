"""Span record. All limits live here so one place defines what can be recorded."""

from __future__ import annotations

import math
import time
from typing import Any

MAX_NAME_LEN = 128
MAX_KEY_LEN = 64
MAX_ATTRIBUTES = 32
MAX_STR_LEN = 256

# Attribute keys that are never recorded in 0.1.0 (prompts, answers, file names,
# raw text, credentials). Matching is case-insensitive and treats "-" and "_" alike.
# This is defense in depth against accidents, NOT a guarantee: a value stored
# under an innocent key is recorded as given. Do not put secrets or personal
# data in attributes.
DENIED_KEYS = frozenset(
    {
        "prompt",
        "system_prompt",
        "answer",
        "completion",
        "response",
        "content",
        "text",
        "message",
        "messages",
        "query",
        "question",
        "file_name",
        "filename",
        "file",
        "path",
        "document",
        "body",
        "password",
        "secret",
        "token",
        "api_key",
        "authorization",
    }
)


def _cut(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit]


def _norm_key(key: str) -> str:
    return key.strip().lower().replace("-", "_")


class Span:
    """One timed operation. Created by Tracer.span(); do not build directly."""

    __slots__ = (
        "name",
        "trace_id",
        "span_id",
        "parent_id",
        "start_time",
        "duration_ms",
        "status",
        "error_type",
        "attributes",
        "dropped_attributes",
        "_t0",
    )

    def __init__(self, name: str, trace_id: str, span_id: str, parent_id: str | None):
        self.name = _cut(str(name), MAX_NAME_LEN)
        self.trace_id = trace_id
        self.span_id = span_id
        self.parent_id = parent_id
        self.start_time = time.time()
        self._t0 = time.perf_counter()
        self.duration_ms: float | None = None
        self.status = "ok"
        self.error_type: str | None = None
        self.attributes: dict[str, Any] = {}
        self.dropped_attributes = 0

    def set_attribute(self, key: Any, value: Any) -> bool:
        """Record one attribute. Returns True if kept, False if dropped (and counted)."""
        if not isinstance(key, str) or not key:
            self.dropped_attributes += 1
            return False
        key = _cut(key, MAX_KEY_LEN)
        if _norm_key(key) in DENIED_KEYS:
            self.dropped_attributes += 1
            return False
        # bool is a subclass of int; both are allowed. Nothing else is: no repr() of objects.
        if not isinstance(value, (str, int, float, bool)):
            self.dropped_attributes += 1
            return False
        if isinstance(value, float) and not math.isfinite(value):
            self.dropped_attributes += 1
            return False
        if key not in self.attributes and len(self.attributes) >= MAX_ATTRIBUTES:
            self.dropped_attributes += 1
            return False
        if isinstance(value, str):
            value = _cut(value, MAX_STR_LEN)
        self.attributes[key] = value
        return True

    def _finish(self, exc: BaseException | None) -> None:
        self.duration_ms = round((time.perf_counter() - self._t0) * 1000, 3)
        if exc is not None:
            self.status = "error"
            # Class name only: exception messages can contain user content.
            self.error_type = _cut(type(exc).__name__, MAX_NAME_LEN)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "start_time": self.start_time,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "error_type": self.error_type,
            "attributes": dict(self.attributes),
            "dropped_attributes": self.dropped_attributes,
        }
