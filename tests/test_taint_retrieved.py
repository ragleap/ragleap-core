"""
Answers built from retrieved documents are untrusted content: a run that follows one starts tainted,
so outbound actions need owner approval even in full autonomy mode. These tests cover the rule itself,
the legacy single-action path, and the opt-out. The chat wiring is tested in tests/test_chat.py and the
end-to-end result in tests/test_stress_office.py.
"""
from unittest.mock import patch

import pytest

from core import agent_loop
from core.employees import actions as act

SLACK_PLAN = {"tool": "send_slack", "channel": "slack", "target": "slack", "content": "hello", "subject": ""}
CODE_PLAN = {"tool": "run_code", "channel": "code", "target": "", "content": "print(1)", "subject": ""}


def test_documents_taint_a_run_by_default():
    assert agent_loop.taint_for_retrieval([{"document_name": "d"}]) is True


def test_no_documents_no_taint():
    assert agent_loop.taint_for_retrieval([]) is False
    assert agent_loop.taint_for_retrieval(None) is False


@pytest.mark.parametrize("value", ["false", "FALSE", "0", "no", "off"])
def test_the_taint_can_be_turned_off(monkeypatch, value):
    monkeypatch.setenv("TAINT_RETRIEVED_DOCUMENTS", value)
    assert agent_loop.taint_for_retrieval([{"document_name": "d"}]) is False


def test_run_action_passes_force_semi_to_the_gate():
    with patch("core.autonomy.execute_or_request", return_value={"status": "pending_approval"}) as gate:
        act.run_action(SLACK_PLAN, None)
        assert gate.call_args.kwargs["force_semi"] is False
        act.run_action(SLACK_PLAN, None, force_semi=True)
        assert gate.call_args.kwargs["force_semi"] is True


def test_legacy_path_forces_approval_for_outbound_tools_after_documents():
    with patch.object(act, "plan_action", return_value=SLACK_PLAN), \
         patch("core.autonomy.execute_or_request", return_value={"status": "pending_approval"}) as gate:
        act.maybe_act("q", "a", None, None, tainted=True)
        assert gate.call_args.kwargs["force_semi"] is True
        act.maybe_act("q", "a", None, None)
        assert gate.call_args.kwargs["force_semi"] is False


def test_legacy_path_does_not_force_non_outbound_tools():
    with patch.object(act, "plan_action", return_value=CODE_PLAN), \
         patch("core.autonomy.execute_or_request", return_value={"status": "pending_approval"}) as gate:
        act.maybe_act("q", "a", None, None, tainted=True)
        assert gate.call_args.kwargs["force_semi"] is False
