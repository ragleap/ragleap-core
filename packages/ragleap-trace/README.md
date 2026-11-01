# ragleap-trace

Dependency-free tracing for RagLeap. Standard library only (`dependencies = []`).
Version 0.1.0 is the core: spans, a tracer, two exporters. It does **not** yet
connect to ragleap-rag (that is milestone M3).

```python
from ragleap_trace import Tracer, JsonlExporter, traced

tracer = Tracer([JsonlExporter("traces.jsonl")])

with tracer.span("retrieve", top_k=5) as s:
    s.set_attribute("hits", 4)

@traced(tracer)
async def answer(...): ...
```

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
- Verified on Python 3.12 in a sandbox so far; CI on 3.10 and 3.12 is configured but not yet run. Behaviour with ragleap-rag, streaming, threads handing off spans, and Windows file permissions is **not verified**.

## Development

```
pip install -e ".[test]"
pytest
python scripts/mutation_check.py   # breaks each protection in a temp copy; every break must be caught
```
