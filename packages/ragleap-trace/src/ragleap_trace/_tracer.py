"""Tracer: span context manager (sync and async) and @traced decorator."""

from __future__ import annotations

import contextvars
import functools
import inspect
import secrets
import threading
from typing import Any, Callable, Iterable

from ._span import Span

_current: contextvars.ContextVar[Span | None] = contextvars.ContextVar(
    "ragleap_trace_current", default=None
)


class Tracer:
    """Creates spans and hands finished spans to exporters.

    Exporter failures never reach the caller; they are counted in `dropped`.
    """

    def __init__(self, exporters: Iterable[Any] | None = None):
        self.exporters = list(exporters or [])
        self.dropped = 0
        self._lock = threading.Lock()

    def span(self, name: str, **attributes: Any) -> "_SpanContext":
        return _SpanContext(self, name, attributes)

    def _emit(self, span: Span) -> None:
        data = span.to_dict()
        for exporter in self.exporters:
            try:
                exporter.export(data)
            except Exception:  # never propagate into the traced code
                with self._lock:
                    self.dropped += 1


class _SpanContext:
    def __init__(self, tracer: Tracer, name: str, attributes: dict[str, Any]):
        self._tracer = tracer
        self._name = name
        self._attributes = attributes
        self._span: Span | None = None
        self._token: contextvars.Token | None = None

    def _start(self) -> Span:
        parent = _current.get()
        trace_id = parent.trace_id if parent else secrets.token_hex(16)
        span = Span(
            self._name, trace_id, secrets.token_hex(8), parent.span_id if parent else None
        )
        for key, value in self._attributes.items():
            span.set_attribute(key, value)
        self._span = span
        self._token = _current.set(span)
        return span

    def _end(self, exc: BaseException | None) -> None:
        span = self._span
        if span is None:
            return
        if self._token is not None:
            try:
                _current.reset(self._token)
            except ValueError:
                _current.set(None)
        span._finish(exc)
        self._tracer._emit(span)

    def __enter__(self) -> Span:
        return self._start()

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._end(exc)
        return False  # never swallow the caller's exception

    async def __aenter__(self) -> Span:
        return self._start()

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self._end(exc)
        return False


def traced(tracer: Tracer, name: str | None = None) -> Callable:
    """Decorator: wrap a sync or async function in a span named after it."""

    def decorate(fn: Callable) -> Callable:
        span_name = name or fn.__qualname__
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def awrapper(*args, **kwargs):
                async with tracer.span(span_name):
                    return await fn(*args, **kwargs)

            return awrapper

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with tracer.span(span_name):
                return fn(*args, **kwargs)

        return wrapper

    return decorate
