"""Tenant scope for runs, approvals and audit (design: #1395). A scripted model replaces the LLM everywhere;
nothing here touches a network."""
import json
import re
import threading

import pytest
from ragleap_security import AuditError, AuditLog, RateLimiter, RateLimitError, Scope, ScopeError, verify
from ragleap_tools import Tool, ToolResult

from ragleap_agents import (
    TRUSTED,
    Agent,
    InMemoryStateStore,
    Policy,
    ResumeError,
    SQLiteStateStore,
    ToolPolicy,
    delegate_policy,
    make_delegate_tool,
)

DONE = json.dumps({"tool": "done"})
FULL = Scope("acme", subject="alice", agent="bot", session="s1")
OTHER = Scope("globex")
SECRET_TASK = "pay invoice 4711 for the ACME payroll"
SECRET_ARG = "Wire 50,000 to account 12-3456"


def plan(tool, **arguments):
    return json.dumps({"tool": tool, "arguments": arguments})


class Script:
    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if not self.replies:
            raise RuntimeError("no scripted reply left")
        return self.replies.pop(0)


def _schema(**props):
    return {"type": "object", "properties": {k: {"type": v} for k, v in props.items()}, "required": list(props)}


def gated(replies, store=None, **agent_kw):
    """An agent whose only tool, send, needs approval. Returns (agent, model, log of sent messages)."""
    log = []
    send = Tool("send", "Send a message", _schema(message="string"),
                lambda message: (log.append(message), ToolResult(True, "sent"))[1])
    pol = Policy(tools={"send": ToolPolicy(False, False, requires_approval=True)})
    llm = Script(replies)
    return Agent(llm, [send], pol, store or InMemoryStateStore(), summarize=False, **agent_kw), llm, log


def paused(scope=FULL, **agent_kw):
    ag, llm, log = gated([plan("send", message=SECRET_ARG), DONE], **agent_kw)
    r = ag.run(SECRET_TASK, scope=scope)
    assert r.status == "awaiting_approval"
    return ag, r, log


def approve(r, value=True):
    return {r.pending["call_id"]: value}


# ---------------------------------------------------------------- the reproduced gap

def test_another_tenant_cannot_approve_or_reject_a_paused_run_and_nothing_runs():
    ag, r, log = paused(scope=Scope("acme"))
    for decision in (True, False):
        with pytest.raises(ResumeError, match="^unknown run$"):
            ag.resume(r.run_id, approve(r, decision), scope=OTHER)
    assert log == [] and ag.store.load(r.run_id)["status"] == "awaiting_approval"
    done = ag.resume(r.run_id, approve(r), scope=Scope("acme"))       # the owner is unaffected
    assert done.status == "done" and log == [SECRET_ARG]


@pytest.mark.parametrize("scope", [
    Scope("globex", subject="alice", agent="bot", session="s1"),     # other tenant
    Scope("acme", subject="bob", agent="bot", session="s1"),         # other subject
    Scope("acme", subject="alice", agent="other", session="s1"),     # other agent
    Scope("acme", subject="alice", agent="bot", session="s2"),       # other session
    Scope("acme", subject="alice", agent="bot"),                     # a broader scope
    Scope("acme"),                                                   # the whole tenant
    None,                                                            # no scope at all
])
def test_every_part_of_the_scope_must_match_exactly(scope):
    ag, r, log = paused(scope=FULL)
    with pytest.raises(ResumeError, match="^unknown run$"):
        ag.resume(r.run_id, approve(r), scope=scope)
    assert log == []
    assert ag.resume(r.run_id, approve(r), scope=FULL).status == "done"      # positive control


def test_a_wrong_tenant_learns_nothing_even_with_bad_decisions():
    ag, r, _ = paused()
    with pytest.raises(ResumeError, match="^unknown run$"):
        ag.resume(r.run_id, {}, scope=OTHER)
    with pytest.raises(ResumeError, match="no decision for the pending call"):
        ag.resume(r.run_id, {}, scope=FULL)


def test_a_wrong_scope_an_unknown_run_and_a_damaged_state_give_the_same_error():
    class Damaged(InMemoryStateStore):
        def load(self, run_id):
            if run_id == "damaged":
                raise ValueError("stored state is not valid")
            return super().load(run_id)
    ag, r, _ = paused(store=Damaged())
    seen = []
    for run_id, scope in ((r.run_id, OTHER), ("missing-run", FULL), ("damaged", FULL)):
        with pytest.raises(ResumeError) as ei:
            ag.resume(run_id, approve(r), scope=scope)
        seen.append((type(ei.value), str(ei.value)))
    assert seen == [(ResumeError, "unknown run")] * 3
    with pytest.raises(ResumeError, match="the stored run state is invalid"):     # without a scope: as in 0.5.0
        ag.resume("damaged", approve(r))


def test_a_run_with_a_scope_needs_it_and_a_run_without_one_refuses_one():
    ag, r, log = paused(scope=None)
    with pytest.raises(ResumeError, match="^unknown run$"):
        ag.resume(r.run_id, approve(r), scope=FULL)
    assert log == [] and ag.resume(r.run_id, approve(r)).status == "done"      # as in 0.5.0


def test_a_damaged_scope_in_a_saved_state_is_not_a_match():
    ag, r, log = paused()
    state = ag.store.load(r.run_id)
    state["scope"] = 12345
    ag.store.save(r.run_id, state)
    with pytest.raises(ResumeError, match="^unknown run$"):
        ag.resume(r.run_id, approve(r), scope=FULL)
    assert log == []


def test_the_saved_run_records_the_scope_key():
    ag, r, _ = paused()
    assert ag.store.load(r.run_id)["scope"] == FULL.key
    plain, r2, _ = paused(scope=None)
    assert plain.store.load(r2.run_id)["scope"] is None


# ---------------------------------------------------------------- strict mode and argument checks

def test_require_scope_makes_a_scope_mandatory_for_run_and_resume():
    ag, llm, log = gated([plan("send", message="m"), DONE], require_scope=True)
    with pytest.raises(ValueError, match="a scope is required"):
        ag.run("task")
    assert llm.prompts == []
    r = ag.run("task", scope=FULL)
    with pytest.raises(ValueError, match="a scope is required"):
        ag.resume(r.run_id, approve(r))
    assert ag.resume(r.run_id, approve(r), scope=FULL).status == "done"


@pytest.mark.parametrize("bad", ["acme", {"tenant": "acme"}, 5, ("acme",)])
def test_only_a_real_scope_is_accepted(bad):
    ag, r, _ = paused()
    with pytest.raises(ScopeError):
        ag.run("task", scope=bad)
    with pytest.raises(ScopeError):
        ag.resume(r.run_id, approve(r), scope=bad)


def test_a_subclass_of_scope_is_not_accepted():
    class Forged(Scope):
        pass
    ag, r, _ = paused()
    with pytest.raises(ScopeError):
        ag.resume(r.run_id, approve(r), scope=Forged("acme", subject="alice", agent="bot", session="s1"))


@pytest.mark.parametrize("kw", [{"require_scope": 1}, {"require_scope": "yes"}, {"audit": "a path"},
                                {"audit": object()}, {"rate_limiter": object()}, {"rate_limiter": "slow"}])
def test_bad_agent_options_are_refused(kw):
    with pytest.raises(ValueError):
        gated([DONE], **kw)


# ---------------------------------------------------------------- run ids and old runs

def test_new_run_ids_are_long_random_and_url_safe():
    ag = Agent(Script([]), [], Policy(), InMemoryStateStore(), summarize=False)
    ids = {ag.run("task").run_id for _ in range(500)}
    assert len(ids) == 500
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{22}", i) for i in ids)


def test_a_run_saved_by_0_5_0_still_resumes_without_a_scope_and_is_refused_with_one():
    ag, r, log = paused(scope=None)
    legacy = dict(ag.store.load(r.run_id))
    legacy.pop("scope")                                    # 0.5.0 states have no scope field
    legacy["run_id"] = "0123456789ab"                       # and a 12-character hex id
    ag.store.save("0123456789ab", legacy)
    with pytest.raises(ResumeError, match="^unknown run$"):
        ag.resume("0123456789ab", approve(r), scope=FULL)
    assert log == []
    assert ag.resume("0123456789ab", approve(r)).status == "done" and log == [SECRET_ARG]


# ---------------------------------------------------------------- shared stores

def test_two_tenants_share_one_sqlite_store_without_crossing(tmp_path):
    store = SQLiteStateStore(str(tmp_path / "runs.db"))
    ag, llm, log = gated([plan("send", message="for acme"), plan("send", message="for globex"), DONE, DONE], store=store)
    a = ag.run("task a", scope=Scope("acme"))
    b = ag.run("task b", scope=Scope("globex"))
    assert a.pending["arguments"] == {"message": "for acme"} and b.pending["arguments"] == {"message": "for globex"}
    for run_id, who in ((a.run_id, Scope("globex")), (b.run_id, Scope("acme"))):
        with pytest.raises(ResumeError, match="^unknown run$"):
            ag.resume(run_id, approve(a), scope=who)
    assert log == []
    ag.resume(b.run_id, approve(b), scope=Scope("globex"))
    ag.resume(a.run_id, approve(a), scope=Scope("acme"))
    assert sorted(log) == ["for acme", "for globex"]


def test_one_approval_runs_once_even_with_many_threads_and_a_scope(tmp_path):
    store = SQLiteStateStore(str(tmp_path / "runs.db"))
    ag, r, log = paused(store=store)
    outcomes = []

    def go():
        try:
            outcomes.append(ag.resume(r.run_id, approve(r), scope=FULL).status)
        except ResumeError as e:
            outcomes.append(str(e))
    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert log == [SECRET_ARG] and outcomes.count("done") == 1


# ---------------------------------------------------------------- sub-agents

def _calc_child(scope_store, **kw):
    t = Tool("calc", "Add", _schema(expression="string"), lambda expression: ToolResult(True, "42"))
    return Agent(Script([plan("calc", expression="1"), DONE]), [t], Policy(tools={"calc": TRUSTED}), scope_store,
                 summarize=False, **kw)


def _parent_of(child, store=None, **kw):
    deleg = make_delegate_tool({"calc": child})
    pol = Policy(tools={"delegate": delegate_policy({"calc": child})}, max_steps=4)
    return Agent(Script([plan("delegate", agent="calc", task="add"), DONE]), [deleg], pol, store or InMemoryStateStore(),
                 summarize=False, **kw)


def test_a_child_run_inherits_the_parents_scope_exactly():
    cstore = InMemoryStateStore()
    r = _parent_of(_calc_child(cstore)).run("q", scope=FULL)
    body = json.loads(r.steps[0]["observation"])
    assert r.status == "done" and cstore.load(body["child_run"])["scope"] == FULL.key


def test_a_child_of_a_run_without_a_scope_has_none():
    cstore = InMemoryStateStore()
    r = _parent_of(_calc_child(cstore)).run("q")
    assert cstore.load(json.loads(r.steps[0]["observation"])["child_run"])["scope"] is None


def test_a_strict_child_does_not_run_for_a_parent_without_a_scope():
    cstore = InMemoryStateStore()
    child = _calc_child(cstore, require_scope=True)
    r = _parent_of(child).run("q")
    assert child.llm.prompts == [] and r.steps[0]["observation"].startswith("error")
    assert r.steps[0].get("status") in ("error", "executed")


def test_a_strict_parent_and_child_work_together_with_a_scope():
    cstore = InMemoryStateStore()
    r = _parent_of(_calc_child(cstore, require_scope=True), require_scope=True).run("q", scope=OTHER)
    assert r.status == "done" and cstore.load(json.loads(r.steps[0]["observation"])["child_run"])["scope"] == OTHER.key


# ---------------------------------------------------------------- per-tenant rate limit

def test_a_rate_limiter_is_checked_per_scope_before_the_model_is_called():
    ag, llm, _ = gated([DONE] * 6, rate_limiter=RateLimiter(rate=0.0001, burst=2))
    ag.run("one", scope=Scope("acme"))
    ag.run("two", scope=Scope("acme"))
    calls = len(llm.prompts)
    with pytest.raises(RateLimitError):
        ag.run("three", scope=Scope("acme"))
    assert len(llm.prompts) == calls                                  # refused before any model call
    assert ag.run("one", scope=Scope("globex")).status in ("done", "stopped")      # another tenant is not limited


def test_resume_is_rate_limited_too_and_the_run_stays_paused():
    ag, llm, log = gated([plan("send", message="m"), DONE], rate_limiter=RateLimiter(rate=0.0001, burst=1))
    r = ag.run("task", scope=FULL)
    with pytest.raises(RateLimitError):
        ag.resume(r.run_id, approve(r), scope=FULL)
    assert log == [] and ag.store.load(r.run_id)["status"] == "awaiting_approval"


def test_a_rate_limiter_needs_a_scope():
    ag, llm, _ = gated([DONE], rate_limiter=RateLimiter(rate=1, burst=5))
    with pytest.raises(ValueError, match="needs a scope"):
        ag.run("task")
    assert llm.prompts == []


# ---------------------------------------------------------------- audit

def records(path):
    return [json.loads(line) for line in open(path)]


def test_the_audit_log_records_each_step_with_the_scope_and_never_any_text(tmp_path):
    path = str(tmp_path / "audit.jsonl")
    ag, r, log = paused(audit=AuditLog(path))
    ag.resume(r.run_id, approve(r), scope=FULL)
    recs = records(path)
    assert [x["event"] for x in recs] == ["run.start", "approval.requested", "approval.decided", "tool.executed", "run.end"]
    assert all(x["scope"] == FULL.key and x["data"]["run_id"] == r.run_id for x in recs)
    assert recs[2]["data"]["approved"] is True and recs[3]["data"]["tool"] == "send"
    text = open(path).read()
    assert SECRET_TASK not in text and SECRET_ARG not in text and "sent" not in recs[3]["data"].values()
    assert verify(path).ok


def test_a_rejection_is_audited_and_the_tool_never_runs(tmp_path):
    path = str(tmp_path / "audit.jsonl")
    ag, r, log = paused(audit=AuditLog(path))
    ag.resume(r.run_id, approve(r, False), scope=FULL)
    recs = records(path)
    assert [x["event"] for x in recs] == ["run.start", "approval.requested", "approval.decided", "run.end"]
    assert recs[2]["data"]["approved"] is False and recs[3]["data"]["status"] == "rejected" and log == []


def test_a_run_without_a_scope_is_audited_without_one(tmp_path):
    path = str(tmp_path / "audit.jsonl")
    ag, r, _ = paused(scope=None, audit=AuditLog(path))
    assert all(x.get("scope") in (None, "") for x in records(path)) and records(path)[0]["event"] == "run.start"


class Flaky(AuditLog):
    """An AuditLog that fails for one event, as a full disk would."""

    def __init__(self, path, bad):
        super().__init__(path)
        self.bad = bad

    def append(self, event, data=None, scope=None):
        if event == self.bad:
            raise AuditError("cannot write")
        return super().append(event, data, scope)


def _flaky(tmp_path, bad):
    return gated([plan("send", message=SECRET_ARG), DONE], audit=Flaky(str(tmp_path / "audit.jsonl"), bad))


def test_if_the_audit_log_cannot_be_written_at_the_start_nothing_runs(tmp_path):
    ag, llm, log = _flaky(tmp_path, "run.start")
    with pytest.raises(AuditError):
        ag.run("task", scope=FULL)
    assert llm.prompts == [] and log == []


def test_if_the_approval_request_cannot_be_recorded_the_run_fails_and_the_tool_does_not_run(tmp_path):
    ag, llm, log = _flaky(tmp_path, "approval.requested")
    r = ag.run("task", scope=FULL)
    assert r.status == "failed" and r.stop_reason == "internal_error" and r.pending is None and log == []


def test_if_the_decision_cannot_be_recorded_the_approved_tool_does_not_run(tmp_path):
    ag, llm, log = _flaky(tmp_path, "approval.decided")
    r = ag.run("task", scope=FULL)
    assert r.status == "awaiting_approval"
    done = ag.resume(r.run_id, approve(r), scope=FULL)
    assert done.status == "failed" and done.stop_reason == "internal_error" and log == []
    assert ag.store.load(r.run_id)["status"] == "failed"


def test_if_even_the_final_record_cannot_be_written_the_error_reaches_the_caller(tmp_path):
    ag, llm, log = _flaky(tmp_path, "run.end")
    r = ag.run("task", scope=FULL)                                    # pauses: no run.end yet
    with pytest.raises(AuditError):
        ag.resume(r.run_id, approve(r), scope=FULL)
