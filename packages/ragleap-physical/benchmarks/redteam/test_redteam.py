"""Tests for the red-team harness (issue #1205), version 2. They run on real pytest and on a plain runner (no fixtures, no parametrize).

They prove: the harness is deterministic, it only reports, it is not blind (weakened bridges and a weakened console, defined here, must each be
reported), known gaps are named and never counted, and on the current code nothing gets through. Python 3.10 to 3.12; no real sleeping.
"""
import copy
import json
import os
import re
import tempfile
from pathlib import Path

import pytest

import redteam_harness as h
import ragleap_physical.console as console_module
import ragleap_physical.proposal as proposal_module
from ragleap_physical import Bridge
from ragleap_physical.errors import ApprovalError

HERE = Path(__file__).resolve().parent
SMALL = 6  # attempts per class for the quick runs (a whole run takes about a second)


def factory_for(cls):
    return lambda adapter, env, clock, level, kw: cls(adapter, env, org_id="org_demo", max_capability=level, clock=clock, **kw)


def passed_in(report, cls):
    return report["classes"][cls]["passed"]


# ---- weakened bridges: each is a hole the harness must see (they exist only in this test file)
class SelfApprovingBridge(Bridge):
    def approve(self, proposal, approver):
        if approver == proposal.requested_by:
            rec = self._ledger[proposal.proposal_id]
            rec["approval"] = {"approver": approver, "digest": rec["digest"], "t": self._clock()}
            rec["state"] = "approved"
            return rec["digest"]
        return super().approve(proposal, approver)


class ClampingBridge(Bridge):
    def propose(self, **kw):
        v = kw.get("value")
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf"):
            try:
                kw["value"] = max(-1.0, min(1.0, float(v)))
            except OverflowError:
                pass
        return super().propose(**kw)


class ReusableBridge(Bridge):
    def apply(self, proposal):
        result = super().apply(proposal)
        self._ledger[proposal.proposal_id]["state"] = "approved"  # the approval is not consumed
        return result


class NoExpiryBridge(Bridge):
    def apply(self, proposal):
        real = self._clock
        self._clock = lambda: min(real(), proposal.expires_at)  # the apply never sees the proposal as expired
        try:
            return super().apply(proposal)
        finally:
            self._clock = real


class DeafStopBridge(Bridge):
    def stop(self):  # a stop that does nothing
        pass


class DriftingBridge(Bridge):
    """Its limits view changes from call to call: the harness must notice and report 'limits_changed'."""
    calls = 0

    def envelope_view(self):
        view = super().envelope_view()
        DriftingBridge.calls += 1
        if DriftingBridge.calls > 2:
            view["rate"]["max_commands_per_s"] += 1
        return view


class BrokenBridge(Bridge):
    def propose(self, **kw):
        raise RuntimeError("broken on purpose")


class CrashingBridge(Bridge):
    """Raises an untyped error for any value that is not a float: a refusal that is not clean."""
    def propose(self, **kw):
        if not isinstance(kw.get("value"), float):
            raise RuntimeError("crash on purpose")
        return super().propose(**kw)


class CrashingApproveBridge(Bridge):
    """Raises an untyped error inside approve: the bridge layer itself must call that unclean."""
    def approve(self, proposal, approver):
        raise RuntimeError("crash on purpose")


class ReapprovingBridge(Bridge):
    """Lets a proposal that is already approved be approved again."""
    def approve(self, proposal, approver):
        rec = self._ledger[proposal.proposal_id]
        if rec["state"] == "approved":
            rec["state"] = "validated"
        return super().approve(proposal, approver)


class StopKeepsApprovalsBridge(Bridge):
    """Brings back the old hole of #1338: an approval given before a stop is still usable after the clear."""
    def clear_stop(self):
        saved = {pid: dict(rec) for pid, rec in self._ledger.items() if rec["state"] == "approved"}
        super().clear_stop()
        for pid, rec in saved.items():
            self._ledger[pid].update(rec)


class OverRefusingBridge(Bridge):
    """Refuses every apply, even a legitimate approved one: the sequence oracle and the bridge then disagree."""
    def apply(self, proposal):
        raise ApprovalError("refused on purpose")


class AlwaysEqualHmac:
    """Stands in for the hmac module of the console: every comparison says 'equal' (token check and digest check both off)."""
    @staticmethod
    def compare_digest(a, b):
        return True


def run_with_console_hmac(fake, **kw):
    real = console_module.hmac
    console_module.hmac = fake
    try:
        return h.run(**kw)
    finally:
        console_module.hmac = real


# ---- controls and the unbroken code
def test_controls_pass_on_the_current_code():
    controls = h.controls(h.default_factory)
    assert controls and all(controls.values()), controls


def test_nothing_gets_through_on_the_current_code():
    rep = h.run(seed=1, per_class=SMALL)
    assert rep["controls_ok"]
    assert sorted(rep["classes"]) == sorted(h.CLASSES) and all(rep["classes"][c]["attempts"] > 0 for c in h.CLASSES)
    assert rep["findings"] == [], rep["findings"]
    totals = rep["totals"]
    assert totals["passed"] == 0 and totals["unclean"] == 0 and totals["error"] == 0 and totals["inconclusive"] == 0
    assert totals["attempts"] == totals["refused"] + totals["known"]


# ---- determinism
def test_same_seed_same_report():
    a, b = h.run(seed=5, per_class=SMALL), h.run(seed=5, per_class=SMALL)
    for r in (a, b):
        r.pop("environment")
    assert a == b and a["report_sha256"] == b["report_sha256"]


def test_another_seed_gives_another_report():
    a, b = h.run(seed=5, per_class=SMALL), h.run(seed=6, per_class=SMALL)
    assert a["report_sha256"] != b["report_sha256"]
    assert a["classes"] != b["classes"]  # the generated cases differ, not only the seed written in the report


def test_one_class_alone_gives_the_same_numbers_as_in_the_full_run():
    full = h.run(seed=9, per_class=SMALL)["classes"]
    for cls in ("bad_values", "random_sequences"):
        alone = h.run(seed=9, per_class=SMALL, only=[cls])["classes"]
        assert list(alone) == [cls] and alone[cls] == full[cls], cls


# ---- it only reports
def test_the_harness_only_reports():
    before = copy.deepcopy(h.CONFIG)
    rep = h.run(seed=2, per_class=SMALL)
    assert h.CONFIG == before
    assert not [f for f in rep["findings"] if f["detail"] == "limits_changed"]
    w = h.World(h.default_factory)
    try:
        assert w.limits_unchanged()
    finally:
        w.close()
    source = Path(h.__file__).read_text(encoding="utf-8")
    for private in ("._env", "._cfg", "._ledger", "._stop_flag", "_adapter_held", "Envelope.__init__", "setattr("):
        assert private not in source, private


def test_a_drifting_limit_is_reported_as_an_error():
    DriftingBridge.calls = 0
    rep = h.run(seed=3, per_class=3, factory=factory_for(DriftingBridge), only=["bad_values"])
    assert rep["totals"]["error"] > 0
    assert any(f["detail"] == "limits_changed" for f in rep["findings"])


def test_a_failing_control_is_reported():
    rep = h.run(seed=3, per_class=2, factory=factory_for(BrokenBridge), only=["bad_values"])
    assert rep["controls_ok"] is False
    for name in ("propose_via_bridge", "propose_via_tools", "propose_via_mcp", "full_cycle_verified", "fresh_approval_after_clear_applies"):
        assert rep["controls"][name] is False, name
    assert rep["known_gaps"]["#1339"]["status"].startswith("not probed"), rep["known_gaps"]["#1339"]


# ---- it is not blind: each weakened bridge must be seen
def test_a_requester_who_approves_itself_is_reported():
    rep = h.run(seed=7, per_class=30, factory=factory_for(SelfApprovingBridge), only=["self_approval"])
    assert passed_in(rep, "self_approval") > 0 and rep["findings"][0]["seed"] == 7


def test_a_clamping_bridge_is_reported():
    rep = h.run(seed=7, per_class=30, factory=factory_for(ClampingBridge), only=["bad_values"])
    assert passed_in(rep, "bad_values") > 0
    assert {f["scenario"] for f in rep["findings"]} <= {"range", "bigint"}


def test_a_reusable_approval_is_reported():
    rep = h.run(seed=7, per_class=30, factory=factory_for(ReusableBridge), only=["stale_or_reused_approvals", "replay_after_expiry"])
    assert passed_in(rep, "stale_or_reused_approvals") > 0 and passed_in(rep, "replay_after_expiry") > 0


def test_an_approval_that_never_expires_is_reported():
    rep = h.run(seed=7, per_class=80, factory=factory_for(NoExpiryBridge), only=["stale_or_reused_approvals"])
    assert any(f["scenario"] == "stale_approval" for f in rep["findings"])


def test_a_stop_that_does_nothing_is_reported():
    rep = h.run(seed=7, per_class=30, factory=factory_for(DeafStopBridge), only=["stop_races"])
    scenarios = {f["scenario"] for f in rep["findings"]}
    assert {"stop_before_apply", "stop_in_advance"} <= scenarios, scenarios


def test_an_untyped_error_is_reported_as_an_unclean_refusal():
    rep = h.run(seed=3, per_class=10, factory=factory_for(CrashingBridge), only=["bad_values"])
    assert rep["controls_ok"] and rep["totals"]["unclean"] > 0, rep["totals"]
    assert rep["totals"]["passed"] == 0


def test_an_untyped_error_in_the_bridge_itself_is_reported_as_unclean():
    rep = h.run(seed=3, per_class=10, factory=factory_for(CrashingApproveBridge), only=["self_approval"])
    assert rep["totals"]["unclean"] > 0 and rep["totals"]["passed"] == 0, rep["totals"]


def test_a_second_approval_of_the_same_proposal_is_reported():
    rep = h.run(seed=7, per_class=40, factory=factory_for(ReapprovingBridge), only=["stale_or_reused_approvals"])
    assert any(f["scenario"] == "approve_twice" for f in rep["findings"]), [f["scenario"] for f in rep["findings"]]


def test_an_approval_that_survives_a_stop_is_reported():
    """A stop invalidates every approval given before it (#1338, closed on main): if a bridge brings the hole back, it is a finding, not a known gap."""
    kw = dict(seed=7, factory=factory_for(StopKeepsApprovalsBridge))
    rep = h.run(per_class=40, only=["stale_or_reused_approvals"], **kw)
    assert any(f["scenario"] == "approval_before_stop" for f in rep["findings"]), [f["scenario"] for f in rep["findings"]]
    assert passed_in(h.run(per_class=100, only=["random_sequences"], **kw), "random_sequences") > 0
    assert passed_in(h.run(per_class=40, only=["console_approval"], **kw), "console_approval") > 0
    assert rep["totals"]["known"] == 0


def test_the_sequence_oracle_follows_the_revocation_of_approvals():
    rep = h.run(seed=1, per_class=100, only=["random_sequences"])
    assert rep["totals"]["error"] == 0 and rep["totals"]["passed"] == 0, rep["findings"][:2]


def test_a_bridge_that_refuses_what_the_oracle_allows_is_reported_as_an_error():
    rep = h.run(seed=7, per_class=20, factory=factory_for(OverRefusingBridge), only=["random_sequences"])
    assert rep["totals"]["error"] > 0 and any(f["detail"] == "over_refusal" for f in rep["findings"]), rep["findings"][:3]


def test_control_characters_accepted_by_a_weakened_printable_check_are_reported():
    real = proposal_module._printable
    proposal_module._printable = lambda text: True
    try:
        rep = h.run(seed=7, per_class=30, only=["bidi_and_control"])
    finally:
        proposal_module._printable = real
    assert passed_in(rep, "bidi_and_control") > 0
    assert proposal_module._printable is real


def test_a_console_whose_token_and_digest_checks_say_equal_to_everything_is_reported():
    rep = run_with_console_hmac(AlwaysEqualHmac, seed=7, per_class=30, only=["console_auth", "console_approval"])
    assert console_module.hmac is not AlwaysEqualHmac
    assert passed_in(rep, "console_auth") > 0 and passed_in(rep, "console_approval") > 0, rep["totals"]


def test_a_control_that_raises_is_a_failed_control_not_a_crash():
    real = h.rpc_call

    def boom(*args, **kwargs):
        raise RuntimeError("request could not be built")
    h.rpc_call = boom
    try:
        controls = h.controls(h.default_factory)
    finally:
        h.rpc_call = real
    assert h.rpc_call is real
    assert controls["propose_via_mcp"] is False and controls["propose_via_bridge"] is True, controls


# ---- known gaps are named and never counted
def test_known_gaps_are_named_and_never_counted_as_findings():
    rep = h.run(seed=1, per_class=SMALL)
    gaps = rep["known_gaps"]
    assert set(gaps) == set(h.KNOWN_GAPS) == {"#1339", "linger-limit"}  # #1338 and #1367 are fixed on main: they are no longer known gaps
    assert gaps["#1339"]["status"] in ("open (reproduced)", "not reproduced (closed? remove the marker)"), gaps["#1339"]
    assert "#1367" not in gaps and rep["regressions"]["#1367"]["status"] == "held"  # a regression check now, on every run
    assert gaps["linger-limit"]["status"].startswith(("open", "not reproduced", "not probed")), gaps["linger-limit"]
    assert rep["totals"]["passed"] == 0 and rep["findings"] == []
    seen = sum(g["cases"] for g in gaps.values())
    assert rep["totals"]["known"] == seen  # every 'known' row belongs to exactly one named gap


# ---- the command line and the exit code
def test_a_closed_gap_is_reported_as_not_reproduced():
    rep = h.run(seed=1, per_class=2, factory=factory_for(OverRefusingBridge), only=["bad_values"])  # no command ever reaches the adapter here
    assert rep["known_gaps"]["#1339"]["status"].startswith("not reproduced"), rep["known_gaps"]["#1339"]
    assert rep["known_gaps"]["linger-limit"]["status"].startswith(("open", "not probed"))  # an unrelated gap is untouched


def test_the_cli_writes_a_report_and_returns_the_exit_code():
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "report.json")
        code = h.main(["--seed", "4", "--per-class", "4", "--only", "bad_values", "--json", out])
        with open(out, encoding="utf-8") as fh:
            data = json.load(fh)
    assert code == 0 and data["seed"] == 4 and data["classes"]["bad_values"]["attempts"] > 0
    assert data["schema"] == h.SCHEMA and len(data["report_sha256"]) == 64


def test_the_exit_code_is_1_for_a_finding_and_2_for_a_failed_control():
    real_run = h.run
    base = real_run(seed=1, per_class=2, only=["bad_values"])
    try:
        for change, expected in (({"passed": 1}, 1), ({"unclean": 1}, 1), ({"error": 1}, 1), ({}, 0)):
            rep = copy.deepcopy(base)
            rep["totals"].update({"passed": 0, "unclean": 0, "error": 0})
            rep["totals"].update(change)
            h.run = lambda *a, _rep=rep, **k: _rep
            assert h.main(["--seed", "1", "--per-class", "2"]) == expected, change
        rep = copy.deepcopy(base)
        rep["controls_ok"] = False
        h.run = lambda *a, **k: rep
        assert h.main(["--seed", "1", "--per-class", "2"]) == 2
    finally:
        h.run = real_run


# ---- the invariants mapping
def test_every_class_is_mapped_to_invariants_that_exist_on_the_page():
    assert (HERE / "invariants.json").is_file(), "invariants.json must ship next to the harness"
    inv = h.load_invariants()
    assert inv is not None
    assert sorted(inv["classes"]) == sorted(h.CLASSES)
    assert inv["invariants"] == ["S%d" % i for i in range(1, 14)]
    for cls, ids in inv["classes"].items():
        assert ids and set(ids) <= set(inv["invariants"]), (cls, ids)
    page = HERE.parents[1] / "docs" / "safety-invariants.md"
    if not page.exists():
        pytest.skip("docs/safety-invariants.md is not present")
    sections = set(re.findall(r"^### (S\d+) ", page.read_text(encoding="utf-8"), re.M))
    assert sections == set(inv["invariants"]), sections ^ set(inv["invariants"])


def test_the_report_says_which_invariants_no_class_covers():
    inv = h.load_invariants()
    assert inv is not None
    rep = h.run(seed=1, per_class=2, only=["bad_values"])
    covered = {i for ids in inv["classes"].values() for i in ids}
    assert rep["invariants_not_covered"] == sorted(set(inv["invariants"]) - covered, key=lambda s: int(s[1:]))
    assert {"S3", "S6", "S11", "S12", "S13"} <= set(rep["invariants_not_covered"])
    assert rep["classes"]["bad_values"]["invariants"] == inv["classes"]["bad_values"]


# ---- #1367 is a regression check now: the console serves each connection in its own thread, and the harness checks that it stays so
def _regression_with(stub):
    real = h.creq
    h.creq = stub
    try:
        regs = {}
        h.known_gap_probes(h.default_factory, h.Recorder(1), False, regs)
    finally:
        h.creq = real
    assert h.creq is real
    return regs["#1367"]


def test_1367_is_a_regression_check_and_not_a_known_gap():
    assert "#1367" not in h.KNOWN_GAPS and "#1367" in h.REGRESSIONS
    rep = h.run(seed=1, per_class=SMALL, only=["bad_values"])
    assert rep["regressions"]["#1367"]["status"] == "held", rep["regressions"]
    assert rep["regressed"] == [] and "#1367" not in rep["known_gaps"] and rep["findings"] == []
    assert rep["environment"]["regression_details"]["#1367"]["waited_s"] < 4.5


def test_the_stop_bound_is_an_absolute_number_below_the_console_read_timeout():
    assert 1.0 <= h.STOP_BOUND_S <= 4.5


def test_a_stop_that_is_answered_counts_as_held():
    assert _regression_with(lambda *a, **k: (200, {}))["status"] == "held"
    assert _regression_with(lambda *a, **k: (202, {}))["status"] == "held"


def test_a_stop_that_is_refused_busy_or_not_answered_is_a_regression():
    for answer in ((503, None), (None, None), (500, None), (302, None)):
        got = _regression_with(lambda *a, _x=answer, **k: _x)
        assert got["status"] == "REGRESSED", answer


def test_a_check_that_cannot_run_is_not_a_pass():
    def boom(*args, **kwargs):
        raise RuntimeError("no answer")
    assert _regression_with(boom)["status"] == "not checked"
    assert h.regressed_ids({"x": {"status": "not checked"}}) == ["x"]
    assert h.regressed_ids({"x": {"status": "REGRESSED"}}) == ["x"]
    assert h.regressed_ids({"x": {"status": "held"}}) == []


def test_a_regression_is_a_finding_and_a_nonzero_exit_code():
    real = h.STOP_BOUND_S
    h.STOP_BOUND_S = 0.0  # any wait is now too long: this checks the wiring, not the console
    try:
        rep = h.run(seed=1, per_class=2, only=["bad_values"])
        code = h.main(["--seed", "1", "--per-class", "2", "--only", "bad_values"])
    finally:
        h.STOP_BOUND_S = real
    assert h.STOP_BOUND_S == real
    assert rep["regressed"] == ["#1367"] and rep["regressions"]["#1367"]["status"] == "REGRESSED"
    assert [f["scenario"] for f in rep["findings"]] == ["#1367"] and rep["findings"][0]["verdict"] == "error"
    assert code == 1


def test_the_report_hash_does_not_depend_on_the_measured_wait():
    a = h.run(seed=2, per_class=2, only=["bad_values"])
    b = h.run(seed=2, per_class=2, only=["bad_values"])
    assert a["report_sha256"] == b["report_sha256"]
    assert set(a["regressions"]["#1367"]) == {"description", "status"}  # the measured wait is kept out of the hashed report


def test_the_old_slow_flag_is_still_accepted_and_changes_nothing():
    assert h.main(["--seed", "1", "--per-class", "2", "--only", "bad_values", "--include-slow"]) == 0
