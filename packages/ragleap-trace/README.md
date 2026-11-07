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

## Integration tests

`integration/` runs the adapter against a real `RagLeap` with a real Postgres (pgvector), a fake
embedder and a fake generator (copied from ragleap-rag's own tests, so no paid API is called). It needs
ragleap-rag installed and `RAGLEAP_TEST_DATABASE_URL`. It covers `ask()`, `ask_stream()` run to the end,
a stream stopped early (no `on_answer` fires; the next query replaces the pending one), privacy through
real events, and a contract check that the event keys the adapter relies on still exist.
`python scripts/integration_break.py` breaks the adapter, the tracer and ragleap-rag itself in temp copies
and every break must make the tests fail. These tests are not part of the wheel.

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
- Verified in CI (3.10, 3.12): the core and the adapter's unit tests. The adapter is also verified against a real RagLeap in CI (Python 3.11, Postgres 16 with pgvector, fake embedder and generator; see Integration tests). Not verified: real LLM providers (the usage keys in the tests come from a fake shaped like the real providers), vector backends other than pgvector, spans handed between threads, Windows file permissions.

## Threat model (0.1.0)

What ragleap-trace protects, against what, and what it does not. It is a local library: no network, no server, no stored credentials.

**Assets:** the content that flows through your pipeline (prompts, answers, file names, user text), the availability of your application, and the trace file.

**Trust boundary:** your application code and ragleap-rag are trusted callers, but they can pass sensitive data by accident. Anyone who can read the trace file or the process memory is outside what this library defends.

| Threat | Mitigation in 0.1.0 | Evidence | Residual risk |
|---|---|---|---|
| Content recorded by accident | Denylisted keys are never recorded; the ragleap-rag adapter copies only an allow-list (counts, booleans, provider name, token numbers, lengths); only `str`, `int`, `float` and `bool` values are kept; exceptions are recorded by class name only | Unit and integration tests, each with a break proof | A value under an innocent key is recorded as given |
| Tracing breaks your application | Exporter and handler errors are swallowed and counted in `tracer.dropped`; the caller's own exception always propagates unchanged | Tests and break proofs; real RagLeap integration tests | Exporters run in the caller's thread: a slow exporter slows the caller (no timeout, no background queue) |
| Unbounded data per span | Name 128, key 64, 32 attributes, strings cut at 256 | Tests and break proofs | The JSONL file has no size limit or rotation |
| Trace file readable by others | Created with mode 0600; an existing group- or world-readable file is refused | Tests and break proofs on POSIX | Windows permissions are not verified; no encryption at rest |
| Events paired with the wrong query | Pairing is per thread and per asyncio task | Thread and asyncio tests; real RagLeap integration tests | Two streams interleaved in one thread or task can pair wrongly |
| Silent data loss when ragleap-rag changes an event | Contract tests fail if an event key the adapter relies on disappears | Integration contract tests; breaks simulate renamed keys | Only the keys the adapter uses are checked |
| Supply chain | No runtime dependencies (`dependencies = []`); the CI actions are pinned to commit SHAs; releases are published from the release workflow through PyPI trusted publishing (no stored token) | `pyproject.toml`; the workflow files; the TestPyPI dry run published through trusted publishing, and its files carry a provenance record naming the repository, workflow and environment | You still trust the PyPI account and the GitHub repository; there is no signature beyond that provenance record |

**Out of scope in 0.1.0:** hostile code in the same process, multi-tenant separation (no tenant tagging), retention policies, network exporters, sampling, and opt-in content capture (which would need ragleap-security and fail closed).

## Development

```
pip install -e ".[test]"
pytest
python scripts/mutation_check.py   # breaks each protection in a temp copy; every break must be caught
```
