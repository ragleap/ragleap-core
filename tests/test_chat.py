"""
Tests for core/chat.py's ask() - specifically its wiring to
core.observability.record_trace() (item #2 of the 9-pattern
agentic-architecture build). No prior test file covered core.chat.ask()
at all before this.

Patches chat.GenerationService itself (the class, not just the method) --
GenerationService.__init__ validates a real provider API key immediately
on construction, so mocking only generate_answer() still fails at
`generator = GenerationService()` before the mocked method is ever
reached. Also mocks _prepare() (embedding+retrieval) and record_trace()
itself, so no real network/DB calls happen anywhere in these tests --
record_trace()'s own real-DB behavior is covered separately in
tests/test_observability.py.
"""
import os
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import chat


def _mock_generation_service(return_value):
    mock_instance = MagicMock()
    mock_instance.generate_answer.return_value = return_value
    mock_class = MagicMock(return_value=mock_instance)
    return mock_class


def test_ask_records_a_trace_on_success():
    mock_gen_service = _mock_generation_service({
        "answer": "The refund policy is 30 days.",
        "sources": ["doc1"],
        "provider_used": "gemini",
        "usage": {"prompt_tokens": 120, "completion_tokens": 40, "total_tokens": 160},
        "chunks_sent": 1,
        "fallback_used": False,
    })
    with patch.object(chat, "_prepare", return_value=([{"document_name": "doc1"}], "en", False)), \
         patch.object(chat, "GenerationService", mock_gen_service), \
         patch.object(chat, "record_trace") as mock_trace:
        result = chat.ask("what is the refund policy?")

    assert result["answer"] == "The refund policy is 30 days."
    mock_trace.assert_called_once()
    kwargs = mock_trace.call_args.kwargs
    assert kwargs["query"] == "what is the refund policy?"
    assert kwargs["role"] is None
    assert kwargs["detected_language"] == "en"
    assert kwargs["chunks_retrieved"] == 1
    assert kwargs["chunks_sent"] == 1
    assert kwargs["provider_used"] == "gemini"
    assert kwargs["fallback_used"] is False
    assert kwargs["prompt_tokens"] == 120
    assert kwargs["completion_tokens"] == 40
    assert kwargs["total_tokens"] == 160
    assert kwargs["error"] is None
    assert isinstance(kwargs["latency_ms"], int)
    assert kwargs["latency_ms"] >= 0


def test_ask_records_a_trace_with_role_and_fallback():
    mock_gen_service = _mock_generation_service({
        "answer": "answer text",
        "sources": [],
        "provider_used": "groq",
        "usage": None,
        "chunks_sent": 0,
        "fallback_used": True,
    })
    with patch.object(chat, "_prepare", return_value=([], "ta", False)), \
         patch.object(chat, "_build_system_prompt", return_value=(None, ["mem-1"])), \
         patch.object(chat, "_augment_query_with_reminder", return_value="some question"), \
         patch.object(chat, "GenerationService", mock_gen_service), \
         patch.object(chat, "record_trace") as mock_trace:
        chat.ask("some question", role="support")

    kwargs = mock_trace.call_args.kwargs
    assert kwargs["role"] == "support"
    assert kwargs["detected_language"] == "ta"
    assert kwargs["provider_used"] == "groq"
    assert kwargs["fallback_used"] is True
    assert kwargs["prompt_tokens"] is None  # usage was None


def test_ask_records_a_trace_on_embedding_failure():
    # GenerationService() is constructed before the embedding_failed check
    # in ask() (confirmed by reading the source), so even this early-return
    # path needs it mocked, despite generate_answer() never actually
    # being called on this path.
    with patch.object(chat, "_prepare", return_value=([], "en", True)), \
         patch.object(chat, "GenerationService", MagicMock()), \
         patch.object(chat, "record_trace") as mock_trace:
        result = chat.ask("broken question")

    assert "embedding failed" in result["answer"]
    mock_trace.assert_called_once()
    kwargs = mock_trace.call_args.kwargs
    assert kwargs["error"] == "embedding failed"
    assert kwargs["query"] == "broken question"


def test_ask_records_error_when_all_providers_failed():
    mock_gen_service = _mock_generation_service({
        "answer": "Sorry, I couldn't generate an answer.",
        "sources": [],
        "provider_used": None,
        "usage": None,
        "chunks_sent": 0,
    })
    with patch.object(chat, "_prepare", return_value=([{"document_name": "d"}], "en", False)), \
         patch.object(chat, "GenerationService", mock_gen_service), \
         patch.object(chat, "record_trace") as mock_trace:
        chat.ask("question")

    kwargs = mock_trace.call_args.kwargs
    assert kwargs["error"] == "all providers failed"
    assert kwargs["provider_used"] is None
    assert kwargs["fallback_used"] is False  # missing key defaults falsy via bool(None)


# --- Self-correction/reflection: only runs for SENSITIVE_DOMAIN_ROLES ---

def test_ask_does_not_run_grounding_check_for_non_sensitive_role():
    mock_gen_service = _mock_generation_service({
        "answer": "answer text",
        "sources": [],
        "provider_used": "gemini",
        "usage": None,
        "chunks_sent": 0,
        "fallback_used": False,
    })
    with patch.object(chat, "_prepare", return_value=([], "en", False)), \
         patch.object(chat, "_build_system_prompt", return_value=(None, [])), \
         patch.object(chat, "_augment_query_with_reminder", return_value="some question"), \
         patch.object(chat, "GenerationService", mock_gen_service), \
         patch.object(chat, "record_trace") as mock_trace:
        result = chat.ask("some question", role="support")

    mock_gen_service.return_value.check_grounding.assert_not_called()
    assert result["answer"] == "answer text"  # no caveat appended
    assert mock_trace.call_args.kwargs["reflection_concern"] is None


def test_ask_runs_grounding_check_for_sensitive_role_and_appends_caveat_when_flagged():
    mock_gen_service = _mock_generation_service({
        "answer": "You have a strong case.",
        "sources": [],
        "provider_used": "gemini",
        "usage": None,
        "chunks_sent": 1,
        "fallback_used": False,
    })
    mock_gen_service.return_value.check_grounding.return_value = "legal conclusion not present in source docs"
    with patch.object(chat, "_prepare", return_value=([{"document_name": "d"}], "en", False)), \
         patch.object(chat, "_build_system_prompt", return_value=(None, [])), \
         patch.object(chat, "_augment_query_with_reminder", return_value="do I have a case?"), \
         patch.object(chat, "GenerationService", mock_gen_service), \
         patch.object(chat, "record_trace") as mock_trace:
        result = chat.ask("do I have a case?", role="legal_intake")

    mock_gen_service.return_value.check_grounding.assert_called_once()
    assert "may not be fully supported" in result["answer"]
    assert result["answer"].startswith("You have a strong case.")
    assert mock_trace.call_args.kwargs["reflection_concern"] == "legal conclusion not present in source docs"


def test_ask_runs_grounding_check_for_sensitive_role_but_no_caveat_when_grounded():
    mock_gen_service = _mock_generation_service({
        "answer": "Consult a licensed attorney for advice specific to your situation.",
        "sources": [],
        "provider_used": "gemini",
        "usage": None,
        "chunks_sent": 1,
        "fallback_used": False,
    })
    mock_gen_service.return_value.check_grounding.return_value = None
    with patch.object(chat, "_prepare", return_value=([{"document_name": "d"}], "en", False)), \
         patch.object(chat, "_build_system_prompt", return_value=(None, [])), \
         patch.object(chat, "_augment_query_with_reminder", return_value="do I have a case?"), \
         patch.object(chat, "GenerationService", mock_gen_service), \
         patch.object(chat, "record_trace") as mock_trace:
        result = chat.ask("do I have a case?", role="legal_intake")

    mock_gen_service.return_value.check_grounding.assert_called_once()
    assert "may not be fully supported" not in result["answer"]
    assert mock_trace.call_args.kwargs["reflection_concern"] is None


def test_ask_skips_grounding_check_when_all_providers_failed_even_for_sensitive_role():
    """No point running a grounding check against an answer that's just
    the generic 'all providers failed' message."""
    mock_gen_service = _mock_generation_service({
        "answer": "Sorry, I couldn't generate an answer.",
        "sources": [],
        "provider_used": None,
        "usage": None,
        "chunks_sent": 0,
    })
    with patch.object(chat, "_prepare", return_value=([{"document_name": "d"}], "en", False)), \
         patch.object(chat, "_build_system_prompt", return_value=(None, [])), \
         patch.object(chat, "_augment_query_with_reminder", return_value="do I have a case?"), \
         patch.object(chat, "GenerationService", mock_gen_service), \
         patch.object(chat, "record_trace") as mock_trace:
        chat.ask("do I have a case?", role="healthcare_intake")

    mock_gen_service.return_value.check_grounding.assert_not_called()
    assert mock_trace.call_args.kwargs["reflection_concern"] is None


# --- Chain of thought (item #4): reasoning_mode only for SENSITIVE_DOMAIN_ROLES ---
def test_ask_sensitive_role_uses_reasoning_mode_and_traces_reasoning():
    mock_gen_service = _mock_generation_service({
        "answer": "clean final answer", "sources": ["d"], "provider_used": "gemini",
        "usage": {}, "chunks_sent": 1, "fallback_used": False,
        "reasoning": "step by step notes",
    })
    mock_gen_service.return_value.check_grounding.return_value = None
    with patch.object(chat, "_prepare", return_value=([{"document_name": "d"}], "en", False)), \
         patch.object(chat, "GenerationService", mock_gen_service), \
         patch.object(chat, "_build_system_prompt", return_value=(None, [])), \
         patch.object(chat, "_augment_query_with_reminder", return_value="q?"), \
         patch.object(chat, "record_trace") as mock_trace:
        result = chat.ask("q?", role="legal_intake")

    assert mock_gen_service.return_value.generate_answer.call_args.kwargs["reasoning_mode"] is True
    assert mock_trace.call_args.kwargs["reasoning"] == "step by step notes"
    assert "reasoning" not in result
    assert result["answer"] == "clean final answer"


def test_ask_non_sensitive_uses_no_reasoning_mode():
    mock_gen_service = _mock_generation_service({
        "answer": "a", "sources": [], "provider_used": "gemini",
        "usage": {}, "chunks_sent": 1, "fallback_used": False, "reasoning": None,
    })
    with patch.object(chat, "_prepare", return_value=([{"document_name": "d"}], "en", False)), \
         patch.object(chat, "GenerationService", mock_gen_service), \
         patch.object(chat, "record_trace") as mock_trace:
        chat.ask("q?")

    assert mock_gen_service.return_value.generate_answer.call_args.kwargs["reasoning_mode"] is False
    assert mock_trace.call_args.kwargs["reasoning"] is None


# --- Tree of thought (item #5): tot_mode is opt-in, passed through ask() ---
def test_ask_passes_tot_mode_through_and_defaults_off():
    mock_gen_service = _mock_generation_service({
        "answer": "a", "sources": [], "provider_used": "gemini",
        "usage": {}, "chunks_sent": 1, "fallback_used": False, "reasoning": "TREE OF THOUGHT x",
    })
    with patch.object(chat, "_prepare", return_value=([{"document_name": "d"}], "en", False)), \
         patch.object(chat, "GenerationService", mock_gen_service), \
         patch.object(chat, "record_trace") as mock_trace:
        chat.ask("q?", tot_mode=True)
        assert mock_gen_service.return_value.generate_answer.call_args.kwargs["tot_mode"] is True
        assert mock_trace.call_args.kwargs["reasoning"] == "TREE OF THOUGHT x"
        chat.ask("q?")
        assert mock_gen_service.return_value.generate_answer.call_args.kwargs["tot_mode"] is False


# --- Supervisor (item #6): role="auto" ---
def _auto_mocks(routed_role, method="llm"):
    gen = _mock_generation_service({
        "answer": "a", "sources": [], "provider_used": "gemini",
        "usage": {}, "chunks_sent": 1, "fallback_used": False, "reasoning": None,
    })
    gen.return_value.check_grounding.return_value = None
    route = MagicMock(return_value={"role": routed_role, "reason": "r", "method": method})
    return gen, route


def test_ask_auto_uses_routed_role_and_defaults_to_untrusted():
    gen, route = _auto_mocks("sales")
    with patch.object(chat, "_prepare", return_value=([{"document_name": "d"}], "en", False)), \
         patch.object(chat, "GenerationService", gen), \
         patch.object(chat, "route_task", route), \
         patch.object(chat, "_build_system_prompt", return_value=(None, [])), \
         patch.object(chat, "_augment_query_with_reminder", return_value="q?"), \
         patch.object(chat, "record_trace") as mock_trace:
        result = chat.ask("how much is it?", role="auto")
    assert route.call_args.kwargs["trusted"] is False
    assert mock_trace.call_args.kwargs["role"] == "sales"
    assert result["routed_role"] == "sales" and result["routing_method"] == "llm"


def test_ask_auto_trusted_flag_is_passed_through():
    gen, route = _auto_mocks("manager")
    with patch.object(chat, "_prepare", return_value=([{"document_name": "d"}], "en", False)), \
         patch.object(chat, "GenerationService", gen), \
         patch.object(chat, "route_task", route), \
         patch.object(chat, "_build_system_prompt", return_value=(None, [])), \
         patch.object(chat, "_augment_query_with_reminder", return_value="q?"), \
         patch.object(chat, "record_trace"):
        chat.ask("summarize approvals", role="auto", trusted=True)
    assert route.call_args.kwargs["trusted"] is True


def test_ask_auto_routed_to_sensitive_role_gets_reasoning_mode():
    gen, route = _auto_mocks("legal_intake")
    with patch.object(chat, "_prepare", return_value=([{"document_name": "d"}], "en", False)), \
         patch.object(chat, "GenerationService", gen), \
         patch.object(chat, "route_task", route), \
         patch.object(chat, "_build_system_prompt", return_value=(None, [])), \
         patch.object(chat, "_augment_query_with_reminder", return_value="q?"), \
         patch.object(chat, "record_trace"):
        chat.ask("q?", role="auto", trusted=True)
    assert gen.return_value.generate_answer.call_args.kwargs["reasoning_mode"] is True


def test_ask_without_auto_never_calls_supervisor():
    gen, route = _auto_mocks("sales")
    with patch.object(chat, "_prepare", return_value=([{"document_name": "d"}], "en", False)), \
         patch.object(chat, "GenerationService", gen), \
         patch.object(chat, "route_task", route), \
         patch.object(chat, "record_trace"):
        result = chat.ask("q?")
    route.assert_not_called()
    assert "routed_role" not in result


# --- Sub-agent spawning (item #7): role="team" ---
def test_ask_team_delegates_to_run_team_and_skips_normal_pipeline():
    gen = _mock_generation_service({"answer": "a", "provider_used": "gemini"})
    fake = {"answer": "merged", "routing_method": "team", "team": []}
    with patch.object(chat, "GenerationService", gen), \
         patch.object(chat, "run_team", return_value=fake) as mock_team, \
         patch.object(chat, "_prepare") as mock_prep:
        result = chat.ask("prices and returns?", role="team")
    assert result == fake
    assert mock_team.call_args.args[0] == "prices and returns?"
    assert mock_team.call_args.kwargs["trusted"] is False
    mock_prep.assert_not_called()


def test_ask_without_team_never_calls_run_team():
    gen = _mock_generation_service({
        "answer": "a", "sources": [], "provider_used": "gemini",
        "usage": {}, "chunks_sent": 1, "fallback_used": False, "reasoning": None,
    })
    with patch.object(chat, "_prepare", return_value=([{"document_name": "d"}], "en", False)), \
         patch.object(chat, "GenerationService", gen), \
         patch.object(chat, "run_team") as mock_team, \
         patch.object(chat, "record_trace"):
        chat.ask("q?")
    mock_team.assert_not_called()


# --- LLM action loop (phase 2): allow_actions is opt-in and trusted-only ---
def _action_gen():
    return _mock_generation_service({
        "answer": "base answer", "sources": [], "provider_used": "gemini",
        "usage": {}, "chunks_sent": 1, "fallback_used": False, "reasoning": None,
    })


def _ask_with_actions(maybe_act_return, **kwargs):
    with patch.object(chat, "_prepare", return_value=([{"document_name": "d"}], "en", False)), \
         patch.object(chat, "GenerationService", _action_gen()), \
         patch.object(chat, "maybe_act", return_value=maybe_act_return) as mock_act, \
         patch.object(chat, "record_trace"):
        result = chat.ask("q?", **kwargs)
    return result, mock_act


def test_ask_actions_off_by_default():
    result, mock_act = _ask_with_actions({"status": "executed", "tool": "send_slack"})
    mock_act.assert_not_called()
    assert "action" not in result and result["answer"] == "base answer"


def test_ask_allow_actions_trusted_runs_and_appends_note():
    outcome = {"status": "pending_approval", "tool": "send_slack", "target": "slack", "action_id": "AB12CD34"}
    result, mock_act = _ask_with_actions(outcome, allow_actions=True, trusted=True)
    mock_act.assert_called_once()
    assert result["action"] == outcome
    assert "waiting for owner approval" in result["answer"] and result["answer"].startswith("base answer")


def test_ask_allow_actions_untrusted_never_plans():
    result, mock_act = _ask_with_actions({"status": "executed"}, allow_actions=True)
    mock_act.assert_not_called()
    assert result["action"]["status"] == "skipped"
    assert "trusted caller" in result["answer"]


def test_ask_no_action_proposed_leaves_answer_untouched():
    result, mock_act = _ask_with_actions(None, allow_actions=True, trusted=True)
    mock_act.assert_called_once()
    assert "action" not in result and result["answer"] == "base answer"


def test_ask_marks_document_grounded_answers_as_untrusted_for_actions():
    _result, mock_act = _ask_with_actions({"status": "pending_approval", "tool": "send_slack"},
                                          allow_actions=True, trusted=True)
    assert mock_act.call_args.kwargs["tainted"] is True


def test_ask_document_taint_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("TAINT_RETRIEVED_DOCUMENTS", "false")
    _result, mock_act = _ask_with_actions({"status": "executed", "tool": "send_slack"},
                                          allow_actions=True, trusted=True)
    assert mock_act.call_args.kwargs["tainted"] is False


def test_ask_passes_the_document_taint_to_the_agent_loop(monkeypatch):
    monkeypatch.setenv("AGENT_LOOP_ENABLED", "true")
    with patch.object(chat, "_prepare", return_value=([{"document_name": "d"}], "en", False)), \
         patch.object(chat, "GenerationService", _action_gen()), \
         patch.object(chat.agent_loop, "run", return_value=None) as mock_run, \
         patch.object(chat, "record_trace"):
        chat.ask("q?", allow_actions=True, trusted=True)
    assert mock_run.call_args.kwargs["tainted"] is True
