import json
import time

from conftest import STREAM_ANSWER

SECRET_FILE = "payroll-private.txt"
SECRET_TEXT = "alice-private earns a confidential amount"
SECRET_Q = "what does alice-private earn"


def spans(env, name):
    return [s for s in env.exporter.spans if s["name"] == name]


def ingest(env):
    return env.rag.ingest_text(SECRET_FILE, SECRET_TEXT)


# ---- the three flows through a real RagLeap ----------------------------------


def test_ask_produces_ingest_query_answer_spans(env):
    ingest(env)
    env.rag.ask(SECRET_Q, top_k=3)

    (ing,) = spans(env, "rag.ingest")
    (qry,) = spans(env, "rag.query")
    (ans,) = spans(env, "rag.answer")
    assert ing["attributes"] == {"chunks_stored": 1, "chunks_attempted": 1}
    assert qry["attributes"]["top_k"] == 3 and qry["attributes"]["streaming"] is False
    assert qry["attributes"]["chunks_retrieved"] >= 1
    a = ans["attributes"]
    assert a["paired"] is True and a["provider_used"] == "gemini" and a["streaming"] is False
    assert a["usage_total_tokens"] == 15 and a["guardrail_blocked"] is False
    assert ans["duration_ms"] is not None and ans["duration_ms"] >= 0


def test_ask_stream_to_the_end(env):
    ingest(env)
    assert "".join(env.rag.ask_stream(SECRET_Q)) == STREAM_ANSWER

    (qry,) = spans(env, "rag.query")
    (ans,) = spans(env, "rag.answer")
    assert qry["attributes"]["streaming"] is True
    a = ans["attributes"]
    assert a["streaming"] is True and a["paired"] is True
    assert a["answer_length"] == len(STREAM_ANSWER)
    assert "usage_total_tokens" not in a


def test_ask_stream_stopped_early_fires_no_answer_and_next_query_replaces_it(env):
    ingest(env)
    stream = env.rag.ask_stream(SECRET_Q)
    next(stream)
    stream.close()
    assert len(spans(env, "rag.query")) == 1
    assert spans(env, "rag.answer") == []  # a stream stopped early never fires on_answer

    time.sleep(0.6)  # if the stale query were paired, duration would be >= 600 ms
    env.rag.ask(SECRET_Q)
    (ans,) = spans(env, "rag.answer")
    assert ans["attributes"]["paired"] is True and ans["attributes"]["streaming"] is False
    assert ans["duration_ms"] < 500


# ---- privacy through real events ---------------------------------------------


def test_no_query_answer_text_or_filename_in_any_span(env):
    ingest(env)
    env.rag.ask(SECRET_Q)
    list(env.rag.ask_stream(SECRET_Q))
    text = json.dumps(env.exporter.spans)
    for secret in (SECRET_FILE, "payroll", "alice-private", "confidential", "fake answer"):
        assert secret not in text


# ---- how it combines with the rest of the pipeline ---------------------------


def test_adapter_hooks_work_next_to_a_users_own_hooks(env, new_rag):
    seen = []
    hooks = env.adapter.hooks()
    rag = new_rag(on_query=[seen.append, *hooks["on_query"]], on_answer=hooks["on_answer"])
    rag.ingest_text("a.txt", "some content")
    rag.ask("a question")
    assert len(seen) == 1 and seen[0]["query"] == "a question"
    assert len(spans(env, "rag.query")) == 1 and len(spans(env, "rag.answer")) == 1


def test_broken_exporter_never_breaks_a_real_ask_and_drops_are_counted(env):
    class Broken:
        def export(self, span):
            raise RuntimeError("disk full")

    env.tracer.exporters.append(Broken())
    ingest(env)
    result = env.rag.ask(SECRET_Q)
    assert result["answer"]
    assert env.tracer.dropped >= 3  # ingest, query and answer spans each hit the broken exporter


def test_rag_spans_nest_under_a_wrapping_request_span(env):
    ingest(env)
    with env.tracer.span("request"):
        env.rag.ask(SECRET_Q)
    (request,) = spans(env, "request")
    for name in ("rag.query", "rag.answer"):
        (s,) = spans(env, name)
        assert s["parent_id"] == request["span_id"] and s["trace_id"] == request["trace_id"]


# ---- contract: the event keys the adapter relies on --------------------------


def test_contract_ingest_and_ask_event_keys(env):
    ingest(env)
    env.rag.ask(SECRET_Q)
    assert {"document_id", "filename", "chunks_stored", "chunks_attempted"} <= set(env.raw.ingest[0])
    assert {"query", "hybrid", "rerank", "top_k", "chunks_retrieved", "streaming",
            "query_rewrite"} <= set(env.raw.query[0])
    answer = env.raw.answer[0]
    assert {"query", "provider_used", "usage", "chunks_sent", "guardrail_blocked",
            "streaming"} <= set(answer)
    assert {"prompt_tokens", "completion_tokens", "total_tokens"} <= set(answer["usage"])


def test_contract_stream_event_keys(env):
    ingest(env)
    list(env.rag.ask_stream(SECRET_Q))
    assert {"query", "hybrid", "rerank", "top_k", "chunks_retrieved", "streaming"} <= set(env.raw.query[0])
    assert {"query", "answer_length", "streaming"} <= set(env.raw.answer[0])
