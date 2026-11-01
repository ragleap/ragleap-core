import asyncio
import json
import threading

import pytest

from ragleap_trace import InMemoryExporter, RagTraceAdapter, Tracer

# Event shapes copied from ragleap-rag origin/main (commit c24a43b), __init__.py call sites.
SECRET_Q = "what is the salary of alice-private"
INGEST = {"document_id": "d1", "filename": "payroll-private.pdf", "chunks_stored": 9, "chunks_attempted": 10}
QUERY = {"query": SECRET_Q, "hybrid": True, "rerank": False, "top_k": 5,
         "chunks_retrieved": 4, "streaming": False, "query_rewrite": None}
ANSWER = {"query": SECRET_Q, "provider_used": "openai",
          "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
          "chunks_sent": 4, "guardrail_blocked": False, "streaming": False}
STREAM_QUERY = {"query": SECRET_Q, "hybrid": True, "rerank": False, "top_k": 5,
                "chunks_retrieved": 4, "streaming": True}
STREAM_ANSWER = {"query": SECRET_Q, "answer_length": 321, "streaming": True}


def make():
    exp = InMemoryExporter()
    tracer = Tracer([exp])
    return tracer, RagTraceAdapter(tracer), exp


def by_name(exp, name):
    return [s for s in exp.spans if s["name"] == name]


# ---- tracer.record (core) ---------------------------------------------------


def test_record_emits_finished_span_with_given_duration():
    tracer, _, exp = make()
    tracer.record("evt", duration_ms=12.5, n=1)
    d = exp.spans[0]
    assert d["duration_ms"] == 12.5 and d["attributes"] == {"n": 1} and d["status"] == "ok"


@pytest.mark.parametrize("bad", [-1, float("nan"), float("inf"), True, "5", None])
def test_record_rejects_bad_durations(bad):
    tracer, _, exp = make()
    tracer.record("evt", duration_ms=bad)
    assert exp.spans[0]["duration_ms"] is None


def test_record_becomes_child_of_current_span():
    tracer, _, exp = make()
    with tracer.span("request"):
        tracer.record("evt")
    evt, req = exp.spans
    assert evt["parent_id"] == req["span_id"] and evt["trace_id"] == req["trace_id"]


# ---- shapes and privacy -----------------------------------------------------


def test_ask_flow_records_query_and_answer_with_counts_only():
    tracer, ad, exp = make()
    ad.on_query(QUERY)
    ad.on_answer(ANSWER)
    q = by_name(exp, "rag.query")[0]["attributes"]
    a = by_name(exp, "rag.answer")[0]
    assert q == {"hybrid": True, "rerank": False, "streaming": False, "top_k": 5,
                 "chunks_retrieved": 4, "query_length": len(SECRET_Q)}
    assert a["attributes"] == {"paired": True, "streaming": False, "guardrail_blocked": False,
                               "chunks_sent": 4, "provider_used": "openai",
                               "query_length": len(SECRET_Q), "usage_prompt_tokens": 100,
                               "usage_completion_tokens": 20, "usage_total_tokens": 120}
    assert a["duration_ms"] is not None and a["duration_ms"] >= 0


def test_stream_flow_uses_answer_length():
    tracer, ad, exp = make()
    ad.on_query(STREAM_QUERY)
    ad.on_answer(STREAM_ANSWER)
    a = by_name(exp, "rag.answer")[0]["attributes"]
    assert a["answer_length"] == 321 and a["streaming"] is True and a["paired"] is True
    assert "usage_total_tokens" not in a


def test_ingest_records_counts_never_filename():
    tracer, ad, exp = make()
    ad.on_ingest(INGEST)
    d = exp.spans[0]
    assert d["name"] == "rag.ingest"
    assert d["attributes"] == {"chunks_stored": 9, "chunks_attempted": 10}
    assert "payroll-private" not in json.dumps(exp.spans)


def test_no_query_or_answer_text_reaches_any_span():
    tracer, ad, exp = make()
    for fn, ev in [(ad.on_ingest, INGEST), (ad.on_query, QUERY), (ad.on_answer, ANSWER),
                   (ad.on_query, STREAM_QUERY), (ad.on_answer, STREAM_ANSWER)]:
        fn(ev)
    text = json.dumps(exp.spans)
    assert "alice-private" not in text and "salary" not in text and "payroll" not in text


def test_unexpected_extra_keys_in_events_are_ignored():
    tracer, ad, exp = make()
    ad.on_query({**QUERY, "note": "leak-me", "extra": 5})
    ad.on_answer({**ANSWER, "usage": {**ANSWER["usage"], "secret_note": 7}, "answer": "leak-me"})
    text = json.dumps(exp.spans)
    assert "leak-me" not in text and "secret_note" not in text and "extra" not in text


def test_query_rewrite_only_known_strategies_else_other():
    tracer, ad, exp = make()
    ad.on_query({**QUERY, "query_rewrite": "hyde"})
    ad.on_query({**QUERY, "query_rewrite": "my private strategy text"})
    ad.on_query({**QUERY, "query_rewrite": ["unhashable"]})
    vals = [s["attributes"]["query_rewrite"] for s in by_name(exp, "rag.query")]
    assert vals == ["hyde", "other", "other"]


def test_bool_is_not_accepted_as_a_count():
    tracer, ad, exp = make()
    ad.on_query({**QUERY, "top_k": True, "chunks_retrieved": False})
    attrs = by_name(exp, "rag.query")[0]["attributes"]
    assert "top_k" not in attrs and "chunks_retrieved" not in attrs


def test_usage_none_and_garbage_values():
    tracer, ad, exp = make()
    ad.on_answer({**ANSWER, "usage": None})
    ad.on_answer({**ANSWER, "usage": {"prompt_tokens": "100", "total_tokens": 1.5}})
    for s in by_name(exp, "rag.answer"):
        assert not any(k.startswith("usage_") for k in s["attributes"])


# ---- never raises, counts drops ----------------------------------------------


@pytest.mark.parametrize("garbage", [None, "text", 5, [], object()])
def test_garbage_events_never_raise_and_are_counted(garbage):
    tracer, ad, exp = make()
    ad.on_ingest(garbage)
    ad.on_query(garbage)
    ad.on_answer(garbage)
    assert tracer.dropped == 3


def test_garbage_field_values_do_not_raise():
    tracer, ad, exp = make()
    ad.on_query({"query": object(), "hybrid": "yes", "top_k": None, "query_rewrite": {}})
    ad.on_answer({"provider_used": 5, "usage": "x", "chunks_sent": [], "streaming": 1})
    assert tracer.dropped == 0 and len(exp.spans) == 2


def test_hooks_are_lists_of_callables_under_the_ragleap_parameter_names():
    # ragleap-rag types each hook parameter as Optional[List[Callable[[Dict], None]]]
    _, ad, _ = make()
    hooks = ad.hooks()
    assert set(hooks) == {"on_ingest", "on_query", "on_answer"}
    for handlers in hooks.values():
        assert isinstance(handlers, list) and len(handlers) == 1 and callable(handlers[0])
    hooks["on_query"][0](QUERY)
    hooks["on_answer"][0](ANSWER)
    assert [s["name"] for s in make_spans(ad)] == ["rag.query", "rag.answer"]


def make_spans(ad):
    return ad.tracer.exporters[0].spans


# ---- pairing ----------------------------------------------------------------


def test_answer_without_query_is_unpaired_and_has_no_duration():
    tracer, ad, exp = make()
    ad.on_answer(ANSWER)
    a = exp.spans[0]
    assert a["attributes"]["paired"] is False and a["duration_ms"] is None


def test_pending_is_cleared_after_answer():
    tracer, ad, exp = make()
    ad.on_query(QUERY)
    ad.on_answer(ANSWER)
    ad.on_answer(ANSWER)
    answers = by_name(exp, "rag.answer")
    assert answers[0]["attributes"]["paired"] is True
    assert answers[1]["attributes"]["paired"] is False


def test_stopped_stream_is_replaced_by_next_query():
    tracer, ad, exp = make()
    ad.on_query(STREAM_QUERY)  # consumer stops early: no answer event ever fires
    ad.on_query(QUERY)
    ad.on_answer(ANSWER)
    a = by_name(exp, "rag.answer")[0]
    assert a["attributes"]["paired"] is True and a["attributes"]["streaming"] is False


def test_threads_pair_independently():
    tracer, ad, exp = make()
    a_queried, b_queried = threading.Event(), threading.Event()
    results = {}

    def thread_a():
        ad.on_query(QUERY)
        a_queried.set()
        b_queried.wait(5)
        ad.on_answer({**ANSWER, "provider_used": "A"})

    def thread_b():
        a_queried.wait(5)
        threading.Event().wait(0.1)
        ad.on_query(QUERY)
        b_queried.set()

    ta, tb = threading.Thread(target=thread_a), threading.Thread(target=thread_b)
    ta.start(); tb.start(); ta.join(); tb.join()
    a = [s for s in by_name(exp, "rag.answer") if s["attributes"]["provider_used"] == "A"][0]
    assert a["attributes"]["paired"] is True
    assert a["duration_ms"] >= 80  # A's own start, not B's later start


def test_asyncio_tasks_pair_independently():
    tracer, ad, exp = make()

    async def task_a(a_q, b_q):
        ad.on_query(QUERY)
        a_q.set()
        await b_q.wait()
        ad.on_answer({**ANSWER, "provider_used": "A"})

    async def task_b(a_q, b_q):
        await a_q.wait()
        await asyncio.sleep(0.1)
        ad.on_query(QUERY)
        b_q.set()

    async def main():
        a_q, b_q = asyncio.Event(), asyncio.Event()
        await asyncio.gather(task_a(a_q, b_q), task_b(a_q, b_q))

    asyncio.run(main())
    a = by_name(exp, "rag.answer")[0]
    assert a["duration_ms"] >= 80


def test_events_nest_under_a_user_wrapping_span():
    tracer, ad, exp = make()
    with tracer.span("request"):
        ad.on_query(QUERY)
        ad.on_answer(ANSWER)
    request = by_name(exp, "request")[0]
    for s in exp.spans:
        if s["name"].startswith("rag."):
            assert s["parent_id"] == request["span_id"]
