"""Deliberate-break check for the red-team harness (issue #1205). AI-assisted.

For each row: copy the package source to a scratch folder, change ONE thing, run the harness on the classes that must notice it and require that
the report gets worse there (more 'passed', 'unclean' or 'error' than the unbroken control). A break that is not caught means the harness is blind
to that check. A run that cannot import the package counts as BROKEN, not as caught. The real package is never touched.

  python benchmarks/redteam/breakcheck_redteam.py --package PACKAGE_DIR [--python PY] [--seed 1] [--per-class 60] [--rows 0,3,5] [--check-rows]
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
S = "src/ragleap_physical/"
BR, EN, PR, OB, TL, MC, CO = S + "bridge.py", S + "envelope.py", S + "proposal.py", S + "observation.py", S + "tools.py", S + "mcp_server.py", S + "console.py"
APPLY_EXPIRY = 'if self._clock() > proposal.expires_at:\n            rec["state"] = "expired"\n            self._log("rejected"'
APPROVE_EXPIRY = 'if self._clock() > proposal.expires_at:\n            rec["state"] = "expired"\n            raise ApprovalError("proposal has expired")'
BREAKS = [  # (name, file, old, new, classes that must get worse)
    ("range check off", EN, 'if not lim["pos_min"] <= p.value <= lim["pos_max"]:', "if False:", ["bad_values", "random_sequences"]),
    ("outcome entity check off", EN, "if o.entity != p.target:", "if False:", ["wrong_targets"]),
    ("unit check off", EN, 'if p.value_unit != "rad":', "if False:", ["wrong_targets"]),
    ("episode time check off", EN, 'if o.by_sim_time_s > self._cfg["episode"]["max_sim_time_s"]:', "if False:", ["wrong_targets"]),
    ("self-approval check off", BR, "if _same_identity(approver, proposal.requested_by):", "if False:", ["self_approval", "console_approval", "random_sequences"]),
    ("identity compared as exact text (no case folding)", BR, '    fold = lambda s: unicodedata.normalize("NFKC", s).casefold().strip()\n', "    fold = lambda s: s\n", ["self_approval", "console_approval", "random_sequences"]),
    ("a verified approval is usable again", BR, 'return ApplyResult(proposal.proposal_id, rec["state"], ok, final, digest, self._env.digest)',
     'rec["state"] = "approved"; return ApplyResult(proposal.proposal_id, "verified" if ok else "failed", ok, final, digest, self._env.digest)',
     ["stale_or_reused_approvals", "replay_after_expiry", "console_approval", "random_sequences"]),
    ("apply ignores expiry", BR, APPLY_EXPIRY, APPLY_EXPIRY.replace("if self._clock() > proposal.expires_at:", "if False:", 1),
     ["stale_or_reused_approvals", "replay_after_expiry", "console_approval", "random_sequences"]),
    ("approve ignores expiry", BR, APPROVE_EXPIRY, APPROVE_EXPIRY.replace("if self._clock() > proposal.expires_at:", "if False:", 1), ["replay_after_expiry", "random_sequences"]),
    ("apply does not compare digests", BR, 'if digest != rec["approval"]["digest"] or digest != rec["digest"]:', "if False:", ["stale_or_reused_approvals", "random_sequences"]),
    ("approve does not compare digests", BR, 'if proposal.digest() != rec["digest"]:', "if False:", ["stale_or_reused_approvals"]),
    ("approve twice allowed", BR, 'if rec is None or rec["state"] != "validated":', "if rec is None:", ["stale_or_reused_approvals", "console_approval", "random_sequences"]),
    ("apply without the approval-state check", BR, 'if rec is None or rec["state"] != "approved":', "if rec is None:", ["stale_or_reused_approvals", "console_approval", "random_sequences"]),
    # The check at the start of apply alone is redundant with the re-check after the dry run (removing only one of them changes nothing a client can see),
    # so this row removes both: the stop is then noticed only after the adapter has been called.
    ("stop flag not checked before the adapter call (both checks)", BR,
     ('if self._stop_flag.is_set():\n            self._log("rejected", proposal_id=proposal.proposal_id, code="stopped")',
      "if self._stop_flag.is_set():  # a stop that landed during the dry run: no command reaches the adapter"),
     ('if False:\n            self._log("rejected", proposal_id=proposal.proposal_id, code="stopped")',
      "if False:  # a stop that landed during the dry run: no command reaches the adapter"), ["stop_races", "random_sequences"]),
    ("stop flag ignored between slices", BR, "if self._stop_flag.is_set():\n                raise _StopRequested()", "if False:\n                raise _StopRequested()", ["stop_races"]),
    ("stop that landed during the dry run is not re-checked", BR, "if self._stop_flag.is_set():  # a stop that landed during the dry run: no command reaches the adapter",
     "if False:  # a stop that landed during the dry run: no command reaches the adapter", ["stop_races"]),
    ("printable check off", PR, 'return all(ch == " " or ch in _ALLOWED_FORMAT or unicodedata.category(ch)[0] in "LMNPS" for ch in text)', "return True", ["bidi_and_control"]),
    ("actor id accepts a trailing newline", OB, "not ACTOR_RE.fullmatch(value):", "not ACTOR_RE.match(value):", ["wrong_targets", "bidi_and_control", "self_approval"]),
    ("unknown tool arguments accepted", TL, "    if extra:", "    if False:", ["oversize_or_nested"]),
    ("MCP body size cap off", MC, "if length > MAX_BODY:", "if False:", ["oversize_or_nested"]),
    ("MCP bearer token check off", MC, 'if not hmac.compare_digest(given.encode(), f"Bearer {auth_token}".encode()):', "if False:", ["forbidden_access"]),
    ("MCP host check off", MC, "if not self._host_ok():", "if False:", ["forbidden_access"]),
    ("MCP origin check off", MC, "if origin is not None and origin not in origins:", "if False:", ["forbidden_access"]),
    ("MCP content type check off", MC, 'if not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):', "if False:", ["forbidden_access"]),
    ("MCP deep JSON not caught", MC, "except (ValueError, UnicodeDecodeError, RecursionError):", "except (ValueError, UnicodeDecodeError):", ["oversize_or_nested"]),
    ("console: every request is treated as Alice", CO, "            who = None\n", '            who = "person:alice"\n', ["console_auth"]),
    ("console host check off", CO, 'if (self.headers.get("Host") or "") not in {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}:', "if False:", ["console_auth"]),
    ("console origin check off", CO, "if origin is not None and origin not in origins:", "if False:", ["console_auth"]),
    ("console body size cap off", CO, "if length > MAX_BODY:", "if False:", ["console_input"]),
    ("console content type check off", CO, 'if not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):', "if False:", ["console_input"]),
    ("console accepts a body that is not an object", CO, "if not isinstance(obj, dict):", "if False:", ["console_input"]),
    ("console accepts extra or missing keys", CO, "if set(obj) != keys:", "if False:", ["console_input"]),
    ("console stop accepts a body", CO, "                if obj:", "                if False:", ["console_input"]),
    ("clear-stop does not revoke the approvals given before the stop", BR, 'if was_stopped:\n            for pid, rec in self._ledger.items():', 'if False:\n            for pid, rec in self._ledger.items():',
     ["stale_or_reused_approvals", "console_approval", "random_sequences"]),
    ("console digest check off", CO, "if not hmac.compare_digest(digest.encode(), p.digest().encode()):", "if False:", ["console_approval"]),
]


def run_harness(py, src, seed, per_class, classes, out):
    cmd = [py, str(HERE / "redteam_harness.py"), "--seed", str(seed), "--per-class", str(per_class), "--json", out]
    for c in classes:
        cmd += ["--only", c]
    r = subprocess.run(cmd, env=dict(os.environ, PYTHONPATH=src), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=900)
    try:
        return r.returncode, json.load(open(out, encoding="utf-8"))
    except (OSError, ValueError):
        return r.returncode, None


def bad(rep, cls):
    c = rep["classes"].get(cls, {})
    return c.get("passed", 0) + c.get("unclean", 0) + c.get("error", 0)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--package", required=True)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--per-class", type=int, default=60)
    ap.add_argument("--rows")
    ap.add_argument("--check-rows", action="store_true")
    a = ap.parse_args(argv)
    pkg = Path(a.package)
    problems = [n for n, f, old, new, cl in BREAKS if any((pkg / f).read_text(encoding="utf-8").count(o) != 1 for o in ([old] if isinstance(old, str) else old))]
    for n in problems:
        print("ROW NOT UNIQUE OR MISSING:", n)
    if problems or a.check_rows:
        print("rows checked:", len(BREAKS), "problems:", len(problems))
        return 1 if problems else 0
    sel = [int(x) for x in a.rows.split(",")] if a.rows else list(range(len(BREAKS)))
    with tempfile.TemporaryDirectory() as tmp:
        control_src = os.path.join(tmp, "control", "src")
        shutil.copytree(pkg / "src", control_src, ignore=shutil.ignore_patterns("__pycache__"))
        where = subprocess.run([a.python, "-c", "import ragleap_physical,sys;print(ragleap_physical.__file__)"], env=dict(os.environ, PYTHONPATH=control_src),
                               stdout=subprocess.PIPE, universal_newlines=True).stdout.strip()
        assert where.startswith(control_src), "the harness would test the installed package, not the scratch copy: " + where
        every = sorted({c for i in sel for c in BREAKS[i][4]})
        code, control = run_harness(a.python, control_src, a.seed, a.per_class, every, os.path.join(tmp, "control.json"))
        if control is None or not control["controls_ok"]:
            print("CONTROL RUN FAILED (exit %s): fix this before reading any result" % code)
            return 2
        print("control: python %s, seed %d, per class %d, classes %d: %s" % (control["environment"]["python"], a.seed, a.per_class, len(every), json.dumps(control["totals"], sort_keys=True)))
        caught = 0
        for i in sel:
            name, rel, old, new, classes = BREAKS[i]
            src = os.path.join(tmp, "b%d" % i, "src")
            shutil.copytree(pkg / "src", src, ignore=shutil.ignore_patterns("__pycache__"))
            target = Path(src).parent / rel
            text = target.read_text(encoding="utf-8")
            for o, nw in zip([old] if isinstance(old, str) else old, [new] if isinstance(new, str) else new):
                text = text.replace(o, nw, 1)
            target.write_text(text, encoding="utf-8")
            code, rep = run_harness(a.python, src, a.seed, a.per_class, classes, os.path.join(tmp, "b%d.json" % i))
            if rep is None or not rep["controls_ok"]:
                print("BROKEN  %2d %s (the harness did not run or a control failed, exit %s)" % (i, name, code))
                continue
            worse = {c: (bad(control, c), bad(rep, c)) for c in classes if bad(rep, c) > bad(control, c)}
            if worse:
                caught += 1
                print("caught  %2d %-52s %s" % (i, name, " ".join("%s %d->%d" % (c, x, y) for c, (x, y) in sorted(worse.items()))))
            else:
                print("MISSED  %2d %-52s expected worse in %s" % (i, name, classes))
        print("%d/%d caught" % (caught, len(sel)))
        return 0 if caught == len(sel) else 1


if __name__ == "__main__":
    sys.exit(main())
