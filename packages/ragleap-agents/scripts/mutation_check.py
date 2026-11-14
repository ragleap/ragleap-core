#!/usr/bin/env python3
"""Mutation check for the tenant-scope work in ragleap-agents 0.5.1 (repository only, not part of the installed package).

For each mutation below it copies the package to a temp directory, breaks one protection in the
copy, and runs the test suite. A mutation is KILLED if a test fails, SURVIVED if every test still
passes (meaning that protection is not really tested). The real source is never modified.

  python scripts/mutation_check.py

Needs ragleap-tools and ragleap-security importable (installed, or on PYTHONPATH). Exit code 0 only if the
unmodified suite passes and every mutation is killed. Run from the package directory. These mutations cover
the 0.5.1 scope, rate limit, audit and run id changes only; the older protections (taint, approval, delegation)
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
    ('approval.decided is not audited', [(A, '            self._audit(state, "approval.decided", tool=pending["tool"], call_id=call_id, approved=approved)\n', '            pass\n')]),
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
