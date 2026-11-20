"""Breaks one protection at a time in a temporary copy of the package and requires a test to fail.

    python scripts/mutation_check.py [--results FILE]

Killed = a test failed (the protection is tested). SURVIVED = nothing failed: a missing test. Every anchor must
match exactly once, so a refactor that moves code makes this script fail loudly instead of passing quietly.
Resumable: finished mutations are read back from the results file and skipped.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile

R, F = "src/ragleap_flows/runner.py", "src/ragleap_flows/flow.py"

MUTATIONS = [
    # --- runner: ownership and scope
    ("owns-always-true", R, 'return isinstance(stored, str) and hmac.compare_digest(stored.encode("utf-8"), scope.key.encode("utf-8"))', "return True"),
    ("owns-prefix-only", R, "hmac.compare_digest(stored.encode(\"utf-8\"), scope.key.encode(\"utf-8\"))", "stored.startswith(scope.key)"),
    ("owns-check-removed", R, "if not isinstance(state, dict) or not self._owns(state, scope):", "if not isinstance(state, dict):"),
    ("flow-digest-check-removed", R, 'if state.get("flow") != self.flow.digest:', "if False:"),
    ("version-check-before-owner", R, 'raise ResumeError("this run belongs to a different version of the flow")', 'raise ResumeError("unknown run")'),
    ("pending-step-not-validated", R, "if step_id not in self.flow.steps:", "if False:"),
    ("resume-status-check-removed", R, 'if state.get("status") != "awaiting_approval" or not pending:', "if not pending:"),
    ("extra-decisions-allowed", R, "if set(decisions) != {step_id}:", "if False:"),
    ("decision-type-unchecked", R, "if not isinstance(approved, bool):", "if False:"),
    ("claim-ignored", R, 'if not claim(state["run_id"], state, "awaiting_approval"):', "if False:"),
    ("rejection-ignored", R, "if not approved:", "if False:"),
    ("approval-not-recorded", R, 'state["approved"].append(step_id)', "pass"),
    ("run-id-short", R, "secrets.token_urlsafe(16)", "secrets.token_urlsafe(4)"),
    ("run-scope-not-saved", R, '"scope": scope.key,', '"scope": "tenant=nobody",'),
    ("scope-not-required", R, "scope = _require_scope(scope)\n        if not isinstance(tainted, bool):", "if not isinstance(tainted, bool):"),
    ("store-for-unchecked", R, 'if not callable(getattr(store, "load", None)) or not callable(getattr(store, "save", None)):', "if False:"),
    ("store-for-ignores-tenant", R, "store = self._store_for(scope.tenant)", "store = self._store_for('shared')"),
    # --- runner: taint and approval
    ("taint-rule-off", R, 'if step.get("outbound") and state["tainted"] and sid not in state["approved"]:', "if False:"),
    ("taint-ignores-input-flag", R, '"tainted": tainted,', '"tainted": False,'),
    ("external-step-does-not-taint", R, 'if step.get("external"):', "if False:"),
    ("forced-approval-reused-for-all", R, 'if step.get("outbound") and state["tainted"] and sid not in state["approved"]:', 'if step.get("outbound") and state["tainted"] and not state["approved"]:'),
    ("approval-step-skipped", R, 'if sid not in state["approved"]:\n                    try:\n                        show', "if False:\n                    try:\n                        show"),
    # --- runner: limits and failure handling
    ("deadline-ignored", R, "if deadline is not None and self._clock() > deadline:", "if False:"),
    ("output-cap-off", R, "if size > MAX_OUTPUT_BYTES:", "if size > MAX_OUTPUT_BYTES * 1000:"),
    ("output-not-json-allowed", R, 'ok, err = False, "output_not_json"', "pass"),
    ("state-cap-off", R, "if _json_size(state) > MAX_STATE_BYTES:", "if _json_size(state) > MAX_STATE_BYTES * 1000:"),
    ("input-cap-off", R, "if _json_size(data) > MAX_INPUT_BYTES:", "if _json_size(data) > MAX_INPUT_BYTES * 1000:"),
    ("rate-limit-ignored", R, "if not decision.allowed:", "if False:"),
    ("tool-failure-continues", R, 'return self._finish(state, store, "failed", "tool_failed:" + sid)', 'state["current"] = step.get("next"); continue'),
    ("success-check-loose", R, "if res.success is not True:", "if not res.success:"),
    ("invalid-result-accepted", R, 'if not hasattr(res, "success") or not hasattr(res, "result"):', "if False:"),
    ("tool-exception-detail-leaks", R, '"tool raised " + type(e).__name__', "str(e)"),
    ("missing-value-continues", R, 'return self._finish(state, store, "failed", "missing_value")\n                ok, value', 'pass\n                ok, value'),
    ("branch-takes-last-match", R, "nxt = case[\"goto\"]\n                        break", "nxt = case[\"goto\"]"),
    ("output-missing-raises", R, 'state["final"], state["stop_reason"] = None, "output_unavailable"\n        self._audit', 'raise Missing()\n        self._audit'),
    ("audit-scope-dropped", R, 'Scope.parse(state["scope"]))', "None)"),
    ("audit-errors-swallowed", R, 'self.audit.append(event, {"run_id": state["run_id"], **data}, Scope.parse(state["scope"]))', 'try:\n            self.audit.append(event, {"run_id": state["run_id"], **data}, Scope.parse(state["scope"]))\n        except Exception:\n            pass'),
    ("audit-leaks-input", R, 'self._audit(state, "flow.start", tainted=tainted, input_bytes=_json_size(data))', 'self._audit(state, "flow.start", tainted=tainted, input=data)'),
    ("config-both-tools-and-dispatch", R, 'if tools is not None and dispatch is not None:', "if False:"),
    ("config-unknown-tool-allowed", R, 'if s["type"] == "tool" and s["tool"] not in tools:', "if False:"),
    # --- flow: validation
    ("cycle-allowed", F, "if state.get(t) == 1:", "if False:"),
    ("unreachable-allowed", F, "if set(state) != ids:", "if False:"),
    ("read-before-run-union", F, "res = set.intersection(*sets) if sets else set()", "res = set.union(*sets) if sets else set()"),
    ("dup-ids-allowed", F, 's["id"] in steps or s["id"] in _RESERVED', 's["id"] in _RESERVED'),
    ("input-id-allowed", F, 's["id"] in steps or s["id"] in _RESERVED', 's["id"] in steps'),
    ("unknown-keys-allowed", F, "if k not in required and k not in optional:", "if False:"),
    ("dup-json-keys-allowed", F, "if k in out:", "if False:"),
    ("nan-in-json-allowed", F, 'raise _fail("NaN and Infinity are not allowed in a flow file")', "return 0"),
    ("version-bool-accepted", F, 'if not _is_int(d["version"]) or d["version"] != 1:', 'if d["version"] != 1:'),
    ("step-cap-off", F, "if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_STEPS:", "if not isinstance(raw, list) or not 1 <= len(raw):"),
    ("file-size-cap-off", F, "if len(canonical(d).encode(\"ascii\")) > MAX_FLOW_BYTES:", "if False:"),
    ("text-size-cap-off", F, 'if len(text.encode("utf-8")) > MAX_FLOW_BYTES:', "if False:"),
    ("prompt-cap-off", F, "1 <= len(s[\"prompt\"]) <= MAX_PROMPT_CHARS", "1 <= len(s[\"prompt\"])"),
    ("nested-value-cap-off", F, "if depth > MAX_DEPTH:\n        raise _fail(\"a value is nested too deeply\")", "if False:\n        raise _fail(\"a value is nested too deeply\")"),
    ("in-needs-list-off", F, 'if c["op"] == "in" and not isinstance(c["value"], list):', "if False:"),
    ("novalue-op-with-value", F, 'if "value" in c:\n            raise _fail("this operator takes no value")', 'if False:\n            raise _fail("this operator takes no value")'),
    # --- flow: paths and conditions
    # dropped as equivalent: parse_path's startswith('$$') clause is also enforced by the segment regex ($ is not allowed)
    ("literal-dollar-not-unescaped", F, "return template[1:]", "return template"),
    ("list-leading-zero-allowed", F, "(len(seg) > 1 and seg[0] == \"0\") or ", ""),
    ("list-out-of-range-allowed", F, "or int(seg) >= len(cur):", ":"),
    ("missing-key-returns-none", F, "if seg not in cur:\n                raise Missing()\n            cur = cur[seg]", "cur = cur.get(seg)"),
    ("missing-satisfies-condition", F, "    except Missing:\n        return False\n    if op ==", "    except Missing:\n        return True\n    if op =="),
    ("bool-equals-int", F, "if isinstance(a, bool) or isinstance(b, bool):\n        return isinstance(a, bool) and isinstance(b, bool) and a == b", "if False:\n        pass"),
    ("bool-compares-as-number", F, "if isinstance(v, bool) or isinstance(ref, bool):\n        return False", "if False:\n        return False"),
    ("string-vs-number-compares", F, "if not (both_num or both_str):", "if False:"),
    ("all-becomes-any", F, 'return all(eval_cond(x, values) for x in c["all"])', 'return any(eval_cond(x, values) for x in c["all"])'),
    ("any-becomes-all", F, 'return any(eval_cond(x, values) for x in c["any"])', 'return all(eval_cond(x, values) for x in c["any"])'),
    ("gt-becomes-ge", F, '"gt": v > ref', '"gt": v >= ref'),
    ("lt-becomes-le", F, '"lt": v < ref', '"lt": v <= ref'),
]


def run_tests(pkg_dir):
    env = dict(os.environ, PYTHONPATH=os.path.join(pkg_dir, "src"))
    p = subprocess.run([sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", "tests"], cwd=pkg_dir,
                       env=env, capture_output=True, text=True, timeout=600)
    return p.returncode, p.stdout[-300:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default=os.path.join(tempfile.gettempdir(), "flows_mutations.tsv"))
    args = ap.parse_args()
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    done = {}
    if os.path.exists(args.results):
        for line in open(args.results):
            name, _, verdict = line.rstrip("\n").partition("\t")
            done[name] = verdict
    names = [m[0] for m in MUTATIONS]
    assert len(set(names)) == len(names), "duplicate mutation names"
    with tempfile.TemporaryDirectory() as tmp:
        work = os.path.join(tmp, "pkg")
        shutil.copytree(here, work, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.egg-info"))
        rc, out = run_tests(work)
        assert rc == 0, "baseline fails: " + out
        for name, rel, old, new in MUTATIONS:
            if name in done:
                continue
            path = os.path.join(work, rel)
            src = open(path).read()
            if src.count(old) != 1:
                print("ANCHOR %s: %r matches %d times" % (name, old[:50], src.count(old)))
                open(args.results, "a").write("%s\tANCHOR\n" % name)
                continue
            open(path, "w").write(src.replace(old, new))
            try:
                rc, _ = run_tests(work)
            finally:
                open(path, "w").write(src)
            verdict = "killed" if rc != 0 else "SURVIVED"
            print("%-36s %s" % (name, verdict))
            open(args.results, "a").write("%s\t%s\n" % (name, verdict))
    verdicts = [l.rstrip("\n").split("\t")[1] for l in open(args.results)]
    print("killed %d of %d; survived %d; anchor problems %d" % (verdicts.count("killed"), len(MUTATIONS),
                                                                  verdicts.count("SURVIVED"), verdicts.count("ANCHOR")))
    return 0 if verdicts.count("killed") == len(MUTATIONS) else 1


if __name__ == "__main__":
    sys.exit(main())
