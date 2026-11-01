"""Adapter from ragleap-rag's observability hooks (on_ingest / on_query / on_answer) to spans.

Event shapes were read from ragleap-rag on origin/main (commit c24a43b). The events carry no
request id and no start event, so this adapter can only measure the time from the query event
to the answer event. For ask() that is generation (plus guardrails and cost tracking); for
ask_stream() it also includes the time the consumer takes between tokens. Retrieval time is
not measurable from events: wrap your call in tracer.span(...) to time the whole request.

Privacy: values are copied from an explicit allow-list (counts, booleans, provider name,
token numbers, lengths). The query text, answer text and file names are never copied.

Pairing uses a ContextVar, so it works per thread and per asyncio task. Known limits:
two streams interleaved inside one thread or task can pair an answer with the wrong query;
a stream the consumer stops early never fires its answer event, and its pending query is
replaced by the next query event in the same context.
"""

from __future__ import annotations

import contextvars
import time
from typing import Any, Callable, Dict, List

from ._tracer import Tracer

_REWRITE_STRATEGIES = frozenset({"contextual", "hyde", "multi_query"})
_USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _len_of(value: Any) -> int | None:
    return len(value) if isinstance(value, str) else None


class RagTraceAdapter:
    """Turns ragleap-rag hook events into ragleap-trace spans. Handlers never raise."""

    def __init__(self, tracer: Tracer) -> None:
        self.tracer = tracer
        self._pending = contextvars.ContextVar(
            f"ragleap_trace_rag_pending_{id(self)}", default=None
        )

    def hooks(self) -> Dict[str, List[Callable[[Any], None]]]:
        """One-item lists keyed by ragleap-rag's parameter names (each takes a list of callables)."""
        return {
            "on_ingest": [self.on_ingest],
            "on_query": [self.on_query],
            "on_answer": [self.on_answer],
        }

    def on_ingest(self, event: Any) -> None:
        self._safe(self._ingest, event)

    def on_query(self, event: Any) -> None:
        self._safe(self._query, event)

    def on_answer(self, event: Any) -> None:
        self._safe(self._answer, event)

    def _safe(self, fn: Callable[[Dict[str, Any]], None], event: Any) -> None:
        try:
            fn(event)
        except Exception:
            self.tracer._drop()

    def _ingest(self, event: Dict[str, Any]) -> None:
        attrs: Dict[str, Any] = {}
        for key in ("chunks_stored", "chunks_attempted"):
            number = _int(event.get(key))
            if number is not None:
                attrs[key] = number
        self.tracer.record("rag.ingest", **attrs)

    def _query(self, event: Dict[str, Any]) -> None:
        query_length = _len_of(event.get("query"))
        self._pending.set(time.perf_counter())
        attrs: Dict[str, Any] = {}
        for key in ("hybrid", "rerank", "streaming"):
            if isinstance(event.get(key), bool):
                attrs[key] = event[key]
        for key in ("top_k", "chunks_retrieved"):
            number = _int(event.get(key))
            if number is not None:
                attrs[key] = number
        rewrite = event.get("query_rewrite")
        if rewrite:
            attrs["query_rewrite"] = (
                rewrite if isinstance(rewrite, str) and rewrite in _REWRITE_STRATEGIES else "other"
            )
        if query_length is not None:
            attrs["query_length"] = query_length
        self.tracer.record("rag.query", **attrs)

    def _answer(self, event: Dict[str, Any]) -> None:
        started = self._pending.get()
        self._pending.set(None)
        elapsed_ms = (time.perf_counter() - started) * 1000 if started is not None else None
        attrs: Dict[str, Any] = {"paired": started is not None}
        for key in ("streaming", "guardrail_blocked"):
            if isinstance(event.get(key), bool):
                attrs[key] = event[key]
        for key in ("chunks_sent", "answer_length"):
            number = _int(event.get(key))
            if number is not None:
                attrs[key] = number
        if isinstance(event.get("provider_used"), str):
            attrs["provider_used"] = event["provider_used"]
        query_length = _len_of(event.get("query"))
        if query_length is not None:
            attrs["query_length"] = query_length
        usage = event.get("usage")
        if isinstance(usage, dict):
            for key in _USAGE_KEYS:
                number = _int(usage.get(key))
                if number is not None:
                    attrs["usage_" + key] = number
        self.tracer.record("rag.answer", duration_ms=elapsed_ms, **attrs)
