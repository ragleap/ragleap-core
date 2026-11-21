#!/usr/bin/env python3
"""Mutation check for the tenant-scope work (0.5.1) and nested approvals (0.6.0) in ragleap-agents (repository only, not part of the installed package).

For each mutation below it copies the package to a temp directory, breaks one protection in the
copy, and runs the test suite. A mutation is KILLED if a test fails, SURVIVED if every test still
passes (meaning that protection is not really tested). The real source is never modified.

  python scripts/mutation_check.py

Needs ragleap-tools and ragleap-security importable (installed, or on PYTHONPATH). Exit code 0 only if the
unmodified suite passes and every mutation is killed. Run from the package directory. These mutations cover
the 0.5.1 scope, rate limit, audit and run id changes and the 0.6.0 nested approvals only; the older protections (taint, approval, delegation)
have no mutation set yet. A killed mutation shows a protection is exercised by a test; it does not show the
protection is sufficient."""
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
import sys
import tempfile
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
A = "src/ragleap_agents/agent.py"
D = "src/ragleap_agents/delegate.py"

MUTATIONS = [
    ('resume ignores the scope', [(A, '        if state is None or not self._owns(state, scope):', '        if state is None:')]),
    ('a wrong scope gets a different error than an unknown run', [(A, '        if state is None or not self._owns(state, scope):\n            raise ResumeError("unknown run")', '        if state is None:\n            raise ResumeError("unknown run")\n        if not self._owns(state, scope):\n            raise ResumeError("run belongs to another scope")')]),
    ('a damaged state with a scope gives a different error', [(A, 'raise ResumeError("unknown run" if scope is not None else "the stored run state is invalid") from None', 'raise ResumeError("the stored run state is invalid") from None')]),
    ('a damaged state without a scope changes its 0.5.0 error', [(A, 'raise ResumeError("unknown run" if scope is not None else "the stored run state is invalid") from None', 'raise ResumeError("unknown run") from None')]),
    ('any scoped run matches any scope', [(A, 'return isinstance(stored, str) and hmac.compare_digest(stored.encode("utf-8"), scope.key.encode("utf-8"))', 'return isinstance(stored, str)')]),
    ('a scope matches by prefix', [(A, 'return isinstance(stored, str) and hmac.compare_digest(stored.encode("utf-8"), scope.key.encode("utf-8"))', 'return isinstance(stored, str) and stored.startswith(scope.key)')]),
    ('a damaged stored scope is not checked', [(A, 'return isinstance(stored, str) and hmac.compare_digest(', 'return hmac.compare_digest(')]),
    ('a call without a scope matches a scoped run', [(A, '        if scope is None:\n            return stored is None', '        if scope is None:\n            return True')]),
    ('strict mode does not require a scope', [(A, '            if self.require_scope:\n                raise ValueError("a scope is required")', '            if False:\n                raise ValueError("a scope is required")')]),
    ('a rate limiter without a scope is allowed', [(A, '            if self.rate_limiter is not None:\n                raise ValueError("a rate limiter needs a scope")', '            if False:\n                raise ValueError("a rate limiter needs a scope")')]),
    ('the scope type is not checked', [(A, '        return _require_scope(scope)', '        return scope')]),
    ('run does not validate its scope argument', [(A, '        scope = self._scope_in(scope)\n        self._limit(scope)\n        state = {', '        self._limit(scope)\n        state = {')]),
    ('resume does not validate its scope argument', [(A, '        scope = self._scope_in(scope)\n        self._limit(scope)\n        try:\n            state = self.store.load(run_id)', '        self._limit(scope)\n        try:\n            state = self.store.load(run_id)')]),
    ('run is not rate limited', [(A, '        self._limit(scope)\n        state = {', '        state = {')]),
    ('resume is not rate limited', [(A, '        self._limit(scope)\n        try:\n            state = self.store.load(run_id)', '        try:\n            state = self.store.load(run_id)')]),
    ('a refused rate limit is ignored', [(A, '        if not decision.allowed:\n            raise RateLimitError(', '        if False:\n            raise RateLimitError(')]),
    ('tenants share one rate limit', [(A, 'decision = self.rate_limiter.check(scope)', 'decision = self.rate_limiter.check("shared")')]),
    ('run ids are 48 bits again', [(A, '"run_id": secrets.token_urlsafe(16),', '"run_id": uuid.uuid4().hex[:12],')]),
    ('run ids are 64 bits', [(A, '"run_id": secrets.token_urlsafe(16),', '"run_id": secrets.token_urlsafe(8),')]),
    ('the scope is not saved with the run', [(A, '"scope": None if scope is None else scope.key,', '"scope": None,')]),
    ('run.start is not audited', [(A, '        self._audit(state, "run.start", tainted=tainted, task_bytes=len(state["task"].encode("utf-8")))\n', '        pass\n')]),
    ('approval.requested is not audited', [(A, '                self._audit(state, "approval.requested", tool=name, call_id=state["pending"]["call_id"])\n', '                pass\n')]),
    ('approval.decided is not audited', [(A, '            self._audit(state, "approval.decided", tool=pending["tool"], call_id=pending["call_id"], approved=approved,\n                        via_depth=via_depth)\n', '            pass\n')]),
    ('tool.executed is not audited', [(A, '        self._audit(state, "tool.executed", tool=tool.name, status=status, forced=forced, obs_bytes=len(obs.encode("utf-8")))\n', '        pass\n')]),
    ('run.end is not audited', [(A, '        self._audit(state, "run.end", status=status, stop_reason=reason, steps=len(state["steps"]))\n', '        pass\n')]),
    ('the audit log holds the tool arguments', [(A, 'forced=forced, obs_bytes=len(obs.encode("utf-8")))', 'forced=forced, arguments=str(args), obs_bytes=len(obs.encode("utf-8")))')]),
    ('the audit log holds the task text', [(A, 'self._audit(state, "run.start", tainted=tainted, task_bytes=len(state["task"].encode("utf-8")))', 'self._audit(state, "run.start", tainted=tainted, task=state["task"])')]),
    ('audit records carry no scope', [(A, 'Scope.parse(key) if key else None)', 'None)')]),
    ('an audit failure is swallowed', [(A, '        self.audit.append(event, {"run_id": state["run_id"], **data}, Scope.parse(key) if key else None)', '        try:\n            self.audit.append(event, {"run_id": state["run_id"], **data}, Scope.parse(key) if key else None)\n        except Exception:\n            pass')]),
    ('the audit option is not type checked', [(A, '        if audit is not None and not isinstance(audit, AuditLog):', '        if False:')]),
    ('require_scope is not type checked', [(A, '        if not isinstance(require_scope, bool):', '        if False:')]),
    ('the rate limiter is not checked for a check method', [(A, '        if rate_limiter is not None and not callable(getattr(rate_limiter, "check", None)):', '        if False:')]),
    ('a child run loses the parent scope', [(D, 'res = child.run(task, tainted=ctx.tainted, scope=ctx.scope)', 'res = child.run(task, tainted=ctx.tainted)')]),
    ('the run context loses the scope', [(A, 'scope=Scope.parse(state["scope"]) if state.get("scope") else None))', 'scope=None))')]),

    # ---- nested approvals (0.6.0, design #963)
    ('a nested pause is not recognised on resume', [(A, '        if pending.get("via"):\n            return self._resume_nested', '        if False:\n            return self._resume_nested')]),
    ('a child run of another scope is accepted', [(A, 'not child._owns(cstate, scope) or ', '')]),
    ('a child run that is not awaiting is accepted', [(A, ' or cstate.get("status") != "awaiting_approval":', ':')]),
    ('the child pending is not compared with the top pending', [(A, 'if not isinstance(cp, dict) or any(cp.get(k) != pending.get(k) for k in ("call_id", "tool", "arguments", "sig")):', 'if not isinstance(cp, dict):')]),
    ('the via path of a middle child is not compared', [(A, 'if cp.get("via") != via[i + 1:]:', 'if False:')]),
    ('the deepest child may still have a via', [(A, '            elif cp.get("via"):', '            elif False:')]),
    # dropped as equivalent: the MAX_VIA check in _load_chain is a cheap guard; a via longer than the depth ceiling cannot match a
    # real child run (the 4th child cannot exist), so an earlier check refuses it anyway and no test can tell the two apart
    ('a delegate tool without nested approval is followed', [(A, ' or tool.nested_approval is not True:', ':')]),
    ('any tool may serve as a delegate on resume', [(A, 'if not isinstance(tool, DelegateTool) or tool.nested_approval is not True:', 'if tool is None:')]),
    ('an unreadable tree is approved anyway', [(A, '            if approved:\n                raise  # fail closed', '            if False:\n                raise')]),
    ('the claim is not atomic', [(A, '            if not claim(state["run_id"], state, "awaiting_approval"):\n                raise ResumeError("run is not awaiting approval")\n        else:\n            agent.store.save', '            if False:\n                raise ResumeError("run is not awaiting approval")\n        else:\n            agent.store.save')]),
    ('a rejection is passed down as an approval', [(A, 'child_res = self._resume_tree(levels, i + 1, approved)', 'child_res = self._resume_tree(levels, i + 1, True)')]),
    ('a rejected child does not end the parent', [(A, 'if child_res.status == "rejected":  # the whole tree', 'if False:  # the whole tree')]),
    ('the executed-before-pause list is missing', [(A, '"observation": "", "executed_before_pause": self._executed_entries(args["agent"], child_res)})', '"observation": ""})')]),
    ('an unreadable child is not marked unavailable', [(A, '"run_id": str(v.get("run_id", ""))[:64], "unavailable": True}', '"run_id": str(v.get("run_id", ""))[:64]}')]),
    ('the executed-before-pause list is not capped', [(A, 'if len(out) > EXECUTED_CAP:', 'if False:')]),
    ('a second pause is not bubbled up again', [(A, 'if child_res.status == "awaiting_approval":  # a second pause deeper down', 'if False:  # a second pause deeper down')]),
    ('child steps are not counted after a resume', [(A, 'counters["child_steps"] += max(0, len(child_res.steps) - int(dele.get("counted", 0)))', 'pass')]),
    ('a forced approval is not shown at the top', [(A, '"forced": cp["forced"], "via": via,', '"forced": False, "via": via,')]),
    ('the parent does not pause when its child paused', [(A, 'if paused is not None:  # a child paused for approval', 'if False:  # a child paused for approval')]),
    ('the loop goes on after a nested pause', [(A, 'if state["status"] == "awaiting_approval":  # a delegate whose child paused: the pause is saved', 'if False:  # a delegate whose child paused: the pause is saved')]),
    ('a decision loop goes on after a second nested pause', [(A, 'if state["status"] == "awaiting_approval":  # the tool was a delegate whose child paused again', 'if False:  # the tool was a delegate whose child paused again')]),
    ('an ancestor decision is not audited', [(A, '            agent._audit(state, "approval.decided", tool=pending["tool"], call_id=pending["call_id"], approved=approved,\n                         via_depth=len(pending["via"]))\n', '            pass\n')]),
    ('the nested pause is not audited', [(A, '            self._audit(state, "approval.requested", tool=state["pending"]["tool"], call_id=state["pending"]["call_id"],\n                        via_depth=len(state["pending"]["via"]))\n            return\n', '            return\n')]),
    ('nested approval is always on', [(D, 'if nested_approval and res.pending:', 'if res.pending:')]),
    ('nested approval is always off', [(D, 'if nested_approval and res.pending:', 'if False:')]),
    ('the nested approval flag is not validated', [(D, 'if not isinstance(nested_approval, bool):', 'if False:')]),
    ('the delegate tool forgets its children', [(D, 'handler, children=kids, nested_approval=nested_approval)', 'handler, children={}, nested_approval=nested_approval)')]),
    ('a second resume is not refused after a nested decision', [(A, '        for agent, st, _ in reversed(levels):  # deepest first\n            self._claim(agent, st)', '        for agent, st, _ in reversed(levels):  # deepest first\n            st["status"], st["pending"] = "running", None')]),
    ('a lost parent taint after a nested resume', [(A, '        if self.policy.for_tool(name).taints:\n            state["tainted"] = True\n        self.store.save(state["run_id"], state)\n        self._audit(state, "tool.executed", tool=name, status="executed"', '        self.store.save(state["run_id"], state)\n        self._audit(state, "tool.executed", tool=name, status="executed"')]),
    ('a nested resume enters the wrong caller context', [(A, 'token = agent._enter(state)  # the child\'s tools must see this run as their caller', 'token = enter_run(current_run() or RunContext(run_id="x", depth=0, tainted=False, delegation={"delegations": 0, "child_steps": 0}))')]),
]

TEST_TIMEOUT_SECONDS = 180
WORKERS = 4


def run_tests(root: Path) -> bool:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
               PYTHONPATH=os.pathsep.join(p for p in (str(root / "src"), os.environ.get("PYTHONPATH", "")) if p))
    try:
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", str(root / "tests")],
                           cwd=root, env=env, capture_output=True, text=True, timeout=TEST_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return False  # a mutation that hangs the suite is detected, so it counts as killed
    return r.returncode == 0


def main() -> int:
    survivors = []
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "base"
        shutil.copytree(PKG, base, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))   # tests read scripts/live_runner.py
        if not run_tests(base):
            print("The unmodified test suite fails; fix that first.")
            return 2
        print("baseline: tests pass", flush=True)

        def one(item):
            i, (name, edits) = item
            work = Path(tmp) / f"m{i}"
            shutil.copytree(base, work)
            try:
                for rel, old, new in edits:
                    p = work / rel
                    text = p.read_text()
                    if text.count(old) != 1:
                        return name, None
                    p.write_text(text.replace(old, new))
                return name, not run_tests(work)
            finally:
                shutil.rmtree(work, ignore_errors=True)

        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for name, killed in pool.map(one, enumerate(MUTATIONS)):
                if killed is None:
                    print(f"ERROR    {name}: anchor not found exactly once", flush=True)
                    survivors.append(name + " (anchor error)")
                    continue
                print(("KILLED   " if killed else "SURVIVED ") + name, flush=True)
                if not killed:
                    survivors.append(name)
    print(f"\n{len(MUTATIONS) - len(survivors)} of {len(MUTATIONS)} mutations killed")
    return 1 if survivors else 0


if __name__ == "__main__":
    sys.exit(main())
