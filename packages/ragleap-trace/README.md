# ragleap-trace

Dependency-free tracing for RagLeap. Standard library only (`dependencies = []`).
Version 0.1.0: spans, a tracer, two exporters, and an adapter for ragleap-rag's
observability hooks (`RagTraceAdapter`).

```python
from ragleap_trace import Tracer, JsonlExporter, traced

tracer = Tracer([JsonlExporter("traces.jsonl")])

with tracer.span("retrieve", top_k=5) as s:
    s.set_attribute("hits", 4)

@traced(tracer)
async def answer(...): ...
```

## Using it with ragleap-rag

```python
from ragleap_trace import Tracer, JsonlExporter, RagTraceAdapter

tracer = Tracer([JsonlExporter("traces.jsonl")])
adapter = RagTraceAdapter(tracer)
hooks = adapter.hooks()   # lists keyed on_ingest / on_query / on_answer
```

`hooks()` returns one-item lists keyed `on_ingest` / `on_query` / `on_answer`, because ragleap-rag
takes a list of callables for each (read from its `RagLeap.__init__` and README). Pass them with
`RagLeap(..., **adapter.hooks())`, or merge them into your own lists. Spans recorded: `rag.ingest`,
`rag.query`, `rag.answer`.

- Only allow-listed values are copied: counts, booleans, provider name, token numbers and the
  query length. Query text, answer text and file names are never copied.
- `query_rewrite` is recorded only as `contextual`, `hyde`, `multi_query` or `other`.
- The events have no request id and no start event. `rag.answer` `duration_ms` is the time from
  the query event to the answer event: generation for `ask()`, and for `ask_stream()` also the
  consumer's time between tokens. Retrieval time is not measurable from events; wrap the call in
  `tracer.span("request")` to time the whole request (the rag spans then nest under it).
- Pairing is per thread and per asyncio task. Two streams interleaved in one thread or task can
  pair an answer with the wrong query. A stream stopped early fires no answer event.
- Handlers never raise; a malformed event is counted in `tracer.dropped`.

## What it guarantees (each has a test and a break proof)

- A span is recorded even when the body raises; the exception still propagates.
- Only the exception **class name** is recorded, never its message.
- Only `str`, `int`, `float`, `bool` attribute values are kept; objects are dropped, never `repr()`'d. NaN/inf are dropped.
- Limits: name 128, key 64, 32 attributes per span, strings cut at 256 characters. Dropped attributes are counted in `dropped_attributes`.
- Keys such as `prompt`, `answer`, `query`, `file_name`, `password`, `token` are never recorded (case-insensitive, `-` equals `_`).
- Exporter errors never reach your code; they are counted in `tracer.dropped`.
- `JsonlExporter` creates files with mode 0600 and refuses an existing file readable by group/others.

## What it does not do (honest limits)

- The key denylist is defense in depth, not a guarantee: a value under an innocent key is recorded as you gave it. Do not put secrets or personal data in attributes.
- No retention or rotation in 0.1.0: the JSONL file grows until you remove it.
- No opt-in content capture, no sampling, no network exporters, no multi-tenant tagging.
- The core is verified on Python 3.10 and 3.12 in CI. Not verified: use with a real ragleap-rag instance (the list form of the hooks was read from its source, not run), a real stream stopped early, spans handed between threads, Windows file permissions.

## Development

```
pip install -e ".[test]"
pytest
python scripts/mutation_check.py   # breaks each protection in a temp copy; every break must be caught
```
