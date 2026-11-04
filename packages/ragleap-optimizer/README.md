# ragleap-optimizer

A measure-first layer that reduces the token use of LLM requests. Alpha. **Not independently audited**: see `docs/threat-model.md` for exactly what is and is not defended.

The idea: "same quality with fewer tokens" is something one application can **measure**, not something a library can promise. So every step reports what it changed and whether it can lose information, and `evaluate` compares quality before and after on your own cases, including where it gets worse. Design: issue #1164.

## What is in 0.1.0

- `Optimizer`: runs steps over a request (a dict with a list of `messages`, and optionally `tools`, `system` and other fields) and returns the new request plus a `Report` with the tokens before and after, per step. A step whose result is longer than its input is discarded. The input is never changed.
- Steps (`ragleap_optimizer.steps`):
  - `dedupe_context()`: removes a long message that is an exact copy of a later one (system messages and short messages are never touched). Nothing is lost.
  - `minify_whitespace()`: three or more newlines become two; a message that is entirely JSON is written compactly. Only spacing changes.
  - `stable_prefix()`: system messages first and tool JSON in sorted key order, so a provider's prompt cache can recognise the start of the request.
  - `trim_tool_output(max_tokens)`: keeps the start and end of an oversized tool message with an explicit marker. **This loses the middle**, so the report says `lossy=True` whenever it cuts.
- `ExactCache`: an in-memory cache that returns an answer only for the same scope and the same request (model, messages, tools and every other field are part of the key). Needs a `Scope` from ragleap-security; entries expire; erase per scope.
- `retry` and `with_fallback`: bounded retries with doubling waits and a deadline, and trying several callables in order.
- `evaluate`: runs your cases with and without the optimizer, scores both answers with your judge, and reports input tokens saved, the quality change with a 95% interval, and every case that got worse. With fewer than 30 cases it gives no savings figure.

Not in 0.1.0: model routing, prompt compression, a semantic cache, secret redaction and framework adapters (planned, see #1164).

## Example

    from ragleap_optimizer import Optimizer, ExactCache, evaluate, steps
    from ragleap_security import Scope

    opt = Optimizer([steps.dedupe_context(), steps.minify_whitespace(), steps.trim_tool_output(max_tokens=800)])
    out = opt.run(request)
    print(out.report.tokens_before, out.report.tokens_after, out.report.lossy)

    cache = ExactCache(ttl_seconds=3600)
    scope = Scope("acme", subject="alice")
    hit, answer = cache.get(scope, out.request)
    if not hit:
        answer = my_llm_call(out.request)
        cache.put(scope, out.request, answer)

    table = evaluate(cases, run=my_llm_call, optimizer=opt, judge=my_judge)
    print(table.summary())

## Limits you should know

- Token counts are **estimates** (one token per 3 UTF-8 bytes, which over-counts English). Pass `counter=` to the `Optimizer` to use a real tokenizer. `evaluate` counts input tokens only, not answers.
- The cache lives in one process and stores whatever you put in it. It does not know whether an answer was good: a wrong answer is served until it expires. Use a short `ttl_seconds` for anything that changes, and never cache answers that depend on the current time or on private context that is not in the request.
- `evaluate` is only as good as your judge and your cases. A small or unrepresentative test set can hide a regression.
- No speed numbers have been measured yet.

## Verifying it yourself

    pip install -e ../ragleap-security -e ".[test]"
    pytest
    python scripts/mutation_check.py   # breaks one protection at a time; every mutation must be killed
