import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "design" / "physical-action-proposals.md"


def _doc():
    return DOC.read_text(encoding="utf-8")


def test_draft_metadata_and_safety_references_are_present():
    text = _doc()
    lines = text.splitlines()

    assert lines[0].startswith("DRAFT:")
    assert re.search(r"^# DRAFT\b", text, re.M)
    assert "- Repository: `ragleap/ragleap-core`" in text
    assert "- Package: `ragleap-physical`" in text
    assert "- Working branch: `docs/physical-action-proposals-v2`" in text
    assert "- Baseline commit: `e1b118ef60159331fbfa96f50dd6fbde22d1bdbe`" in text
    assert "[`docs/safety-invariants.md`](../safety-invariants.md)" in text
    assert "tests/test_safety_invariants.py::test_apply_checks_the_state_again_and_refuses_when_the_episode_limit_was_reached" in text
    assert "AI assistance" in text


def test_mcp_citations_are_versioned_and_unresolved_claims_are_labeled():
    text = _doc()

    assert "2025-06-18" in text
    assert "2025-03-26" in text
    assert "Version Negotiation" in text
    assert "User Interaction Model" in text
    assert re.search(
        r"https://modelcontextprotocol\.io/specification/2025-06-18/basic/lifecycle#version-negotiation",
        text,
    )
    assert re.search(
        r"https://modelcontextprotocol\.io/specification/2025-03-26/basic/lifecycle#version-negotiation",
        text,
    )
    assert re.search(
        r"https://modelcontextprotocol\.io/specification/2025-06-18/server/tools#user-interaction-model",
        text,
    )
    assert "**Untested:**" in text


def test_conformance_contract_and_both_implementations_are_documented():
    text = _doc()

    assert "PhysicalActionTarget" in text
    assert "IndependentFakeTarget" in text
    assert "RagLeap Bridge" in text
    assert "test_target_proposes_and_applies_with_approval" in text
    assert "test_target_refuses_self_approval" in text
    assert "test_target_rejects_tampering_after_approval" in text
    assert "test_target_refuses_apply_after_stop" in text
    assert "test_adapter_conformance.py" in text


def test_known_gaps_and_mutation_evidence_are_accounted_for():
    text = _doc()

    for issue in ("#1337", "#1338", "#1339", "#1367"):
        assert issue in text
    assert "test_stop_invalidates_an_approval_given_before_it" in text
    assert "test_f2_the_console_stop_is_not_held_up_by_a_client_that_never_sends_its_body" in text
    assert "xfail(strict=True" in text
    assert "Deliberate-break proof evidence" in text
    assert "Python 3.11.4; pytest 8.3.5" in text
    assert "192 passed, 108 skipped, 0 failed, 0 xfailed; 14.52 s" in text
    assert "193 passed, 108 skipped, 0 failed, 0 xfailed; 38.19 s" in text
    assert "1 failed, 1 passed" in text
    assert "**REAL**" in text
    assert "**SANDBOX**" in text
    assert "Pending execution" not in text
    for requirement in (
        "Proposal creation",
        "Approval acceptance",
        "Self-approval refusal",
        "Tampering rejection",
        "Stop refusal",
        "Successful result",
    ):
        assert requirement in text


def test_safety_requirements_state_their_test_and_mutation_evidence():
    text = _doc()
    section = text.split("## 4. Safety requirements and evidence", 1)[1].split("\n## 5.", 1)[0]
    statuses = {
        "Proved by deliberate break",
        "Tested, no direct deliberate break yet",
        "Untested",
    }
    header = next(
        line for line in section.splitlines()
        if line.startswith("| Requirement")
    )
    assert [cell.strip() for cell in header.strip("|").split("|")][1] == "Proof status"

    rows = [
        line for line in section.splitlines()
        if line.startswith("| ") and not line.startswith("| Requirement")
        and not re.match(r"^\|\s*-", line)
    ]

    for row in rows:
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        assert len(cells) == 4
        assert cells[1] in statuses
        assert sum(status in row for status in statuses) == 1
        assert cells[2]
        assert cells[3]
    for status in statuses:
        assert status in section

    status_by_requirement = {
        cells[0]: cells[1]
        for cells in (
            [cell.strip() for cell in row.strip("|").split("|")]
            for row in rows
        )
    }
    assert status_by_requirement["Post-dry-run stop re-check"] == "Proved by deliberate break"
    assert status_by_requirement["Initial and in-flight stop enforcement"] == "Tested, no direct deliberate break yet"

    assert "tests/test_bridge.py::test_changed_after_approval_refused" in text
    assert "tests/test_bridge.py::test_expired_approval_refused" in text
    assert "tests/test_bridge.py::test_approval_is_single_use_replay_refused" in text
    assert "tests/test_bridge.py::test_apply_without_approval_refused" in text
    s5_reference = "tests/test_safety_invariants.py::test_apply_checks_the_state_again_and_refuses_when_the_episode_limit_was_reached"
    assert s5_reference in section
    safety_test = (ROOT / "tests" / "test_safety_invariants.py").read_text(encoding="utf-8")
    assert re.search(
        r"^def test_apply_checks_the_state_again_and_refuses_when_the_episode_limit_was_reached\(",
        safety_test,
        re.M,
    )
    assert "adapter.advance(61.0, 100000)" in section
    assert "tests/test_bridge.py::test_dry_run_speed_limit_blocks_before_any_motion" in text
    assert "tests/test_bridge.py::test_full_path_verifies_real_outcome" in text
    assert "tests/test_bridge_limits.py::test_audit_list_is_capped_but_the_sink_gets_everything" in text
    assert "tests/test_known_gaps.py::test_no_adapter_command_when_stop_lands_during_the_dry_run" in text
    assert "Bridge-source mutation proofs" in text
    assert "six `IndependentFakeTarget` mutations" in text
    assert "temporary package copy" in text
    assert "working-tree `bridge.py` SHA-256 remained" in text

    evidence = text.split("## 8. Deliberate-break proof evidence", 1)[1]
    fake_evidence, bridge_evidence = evidence.split("### Bridge-source mutation proofs", 1)
    fake_header = "| Requirement | Corresponding test | One-line temporary mutation | Actual result |"
    bridge_header = "| Requirement | Existing regression test | Temporary Bridge mutation | Actual result |"
    fake_table = fake_evidence.split(fake_header, 1)[1].split("\n\n", 1)[0]
    bridge_table = bridge_evidence.split(bridge_header, 1)[1].split("\n\n", 1)[0]
    fake_rows = [
        row for row in fake_table.splitlines()
        if row.startswith("| ") and not row.startswith("|---")
    ]
    bridge_rows = [
        row for row in bridge_table.splitlines()
        if row.startswith("| ") and not row.startswith("|---")
    ]
    assert len(fake_rows) == 6
    assert len(bridge_rows) == 3
    assert "IndependentFakeTarget" in fake_evidence
    assert all("tests/test_bridge.py::" not in row for row in fake_rows)
    assert all("Temporary Bridge mutation" not in row for row in fake_rows)
    assert all("IndependentFakeTarget" not in row for row in bridge_rows)
