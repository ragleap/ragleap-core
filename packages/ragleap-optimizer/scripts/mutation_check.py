#!/usr/bin/env python3
"""Mutation check for ragleap-optimizer (repository only, not part of the installed package).

For each mutation below it copies the package to a temp directory, breaks one protection in the copy, and runs
the test suite. A mutation is KILLED if a test fails, SURVIVED if every test still passes (that protection is
then not really tested). The real source is never modified.

  python scripts/mutation_check.py

Needs ragleap-security importable. Exit code 0 only if the unmodified suite passes and every mutation is
killed. Run from the package directory. A killed mutation shows a protection is exercised by a test; it does
not show the protection is sufficient."""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
O = "src/ragleap_optimizer/optimizer.py"
ST = "src/ragleap_optimizer/steps.py"
CA = "src/ragleap_optimizer/cache.py"
RE = "src/ragleap_optimizer/resilience.py"
EV = "src/ragleap_optimizer/evaluate.py"
MO = "src/ragleap_optimizer/model.py"

# (name, [(file, old, new), ...]) ; each old string must occur exactly once in its file
MUTATIONS = [
    # --- steps: dedupe_context
    ("dedupe keeps the first copy instead of the last", [(ST, "for i in range(len(msgs) - 1, -1, -1):", "for i in range(len(msgs)):")]),
    ("dedupe removes system messages", [(ST, 'if m.get("role") == "system" or len(text) < min_chars:', "if len(text) < min_chars:")]),
    ("dedupe removes short messages", [(ST, 'if m.get("role") == "system" or len(text) < min_chars:', 'if m.get("role") == "system":')]),
    ("dedupe ignores the role", [(ST, 'key = (m.get("role"), json.dumps(', "key = (0, json.dumps(")]),
    ("dedupe min_chars not validated", [(ST, '_pos_int("min_chars", min_chars)', "pass")]),
    # --- steps: trim_tool_output
    ("trim does not report lossy", [(ST, '                m["content"] = new\n                lossy = True', '                m["content"] = new\n                lossy = False')]),
    ("trim touches every role", [(ST, 'if m.get("role") not in roles or not isinstance(c, str) or counter(c) <= max_tokens:', "if not isinstance(c, str) or counter(c) <= max_tokens:")]),
    ("trim does not check the content is a string", [(ST, 'if m.get("role") not in roles or not isinstance(c, str) or counter(c) <= max_tokens:', 'if m.get("role") not in roles or counter(str(c)) <= max_tokens:')]),
    ("trim does not check that it saves tokens", [(ST, "if counter(new) < counter(c):", "if True:")]),
    ("trim drops the tail", [(ST, 'new = c[:head] + marker + (c[len(c) - tail:] if tail else "")', "new = c[:head] + marker")]),
    ("trim marker counts the wrong number", [(ST, "omitted = len(c) - head - tail", "omitted = len(c) - head")]),
    ("trim acts at the cap", [(ST, "or counter(c) <= max_tokens:", "or counter(c) < max_tokens:")]),
    ("trim max_tokens not validated", [(ST, '_pos_int("max_tokens", max_tokens)', "pass")]),
    ("trim tail fraction not validated", [(ST, "if not isinstance(keep_tail_fraction, (int, float)) or isinstance(keep_tail_fraction, bool) or not 0 <= keep_tail_fraction < 1:", "if False:")]),
    ("trim keeps far more than the cap", [(ST, "budget = max(1, int(len(c) * ratio * 0.9))", "budget = max(1, int(len(c) * ratio * 1.9))")]),
    # --- steps: minify_whitespace
    ("minify does not collapse blank lines", [(ST, 'm["content"] = _BLANKS.sub("\\n\\n", c)', 'm["content"] = c')]),
    ("minify collapses to one newline", [(ST, '_BLANKS.sub("\\n\\n", c)', '_BLANKS.sub("\\n", c)')]),
    ("minify json not compact", [(ST, 'separators=(",", ":")', 'separators=(", ", ": ")')]),
    ("minify escapes non-ASCII", [(ST, "ensure_ascii=False", "ensure_ascii=True")]),
    ("minify touches list content", [(ST, "            if not isinstance(c, str):\n                continue\n            stripped", "            if False:\n                continue\n            stripped")]),
    # --- steps: stable_prefix
    ("stable_prefix leaves system messages where they are", [(ST, 'out["messages"] = [m for m in msgs if m.get("role") == "system"] + [m for m in msgs if m.get("role") != "system"]', 'out["messages"] = msgs')]),
    ("stable_prefix leaves tool keys unsorted", [(ST, 'tools = canon(out["tools"])', 'tools = out["tools"]')]),
    ("stable_prefix ignores sort_tools", [(ST, "            if sort_tools:", "            if False:")]),
    # --- optimizer
    ("a step that bloats the request is kept", [(O, "if t1 > t0:", "if False:")]),
    ("lossy ignores whether anything changed", [(O, "bool(lossy) and changed", "bool(lossy)")]),
    ("lossy never reported", [(O, "bool(lossy) and changed", "False")]),
    ("the callers request is not copied", [(O, "current = copy.deepcopy(request)", "current = request")]),
    ("request not validated", [(O, "        _validate_request(request, self._max_bytes, self._max_messages)\n", "")]),
    ("request size not limited", [(O, 'check_size(text, max_bytes, "request")', "pass")]),
    ("message count not limited", [(O, "if not isinstance(msgs, list) or len(msgs) > max_messages:", "if not isinstance(msgs, list):")]),
    ("counter result not validated", [(O, "if isinstance(n, bool) or not isinstance(n, int) or n < 0:", "if False:")]),
    ("audit does not require a scope", [(O, "scope = require_scope(scope)  # an audit record needs a scope", "pass")]),
    ("audit not written", [(O, '        if self._audit is not None:\n            self._audit.append("optimizer.run"', '        if False:\n            self._audit.append("optimizer.run"')]),
    ("step count not limited", [(O, "if len(steps) > 50 or not all(", "if not all(")]),
    ("step names not checked", [(O, 'isinstance(getattr(s, "step_name", None), str)', "True")]),
    # --- cache
    ("cache key leaves out the scope", [(CA, '{"scope": scope.key, "request": request}', '{"request": request}')]),
    ("cache never expires", [(CA, "if item is None or item[0] <= self._clock():", "if item is None:")]),
    ("cache expiry is off by one", [(CA, "item[0] <= self._clock()", "item[0] < self._clock()")]),
    ("cache hit does not refresh recency", [(CA, "            self._data.move_to_end(key)\n", "            pass\n")]),
    ("cache never evicts", [(CA, "while len(self._data) > self.max_entries:", "while False:")]),
    ("cache evicts the newest", [(CA, "self._drop(next(iter(self._data)))", "self._drop(next(reversed(self._data)))")]),
    ("cache does not copy the value in", [(CA, "copy.deepcopy(value), skey)", "value, skey)")]),
    ("cache does not copy the value out", [(CA, "return True, copy.deepcopy(item[1])", "return True, item[1]")]),
    ("erase removes every scope", [(CA, "keys = list(self._by_scope.get(scope.key, ()))", "keys = list(self._data)")]),
    ("scope index not cleaned on drop", [(CA, "            keys.discard(key)\n", "            pass\n")]),
    ("putting again does not refresh position", [(CA, "            if key in self._data:\n                self._drop(key)", "            if False:\n                self._drop(key)")]),
    ("hits not counted", [(CA, "            self.hits += 1", "            pass")]),
    ("misses not counted", [(CA, "                self.misses += 1", "                pass")]),
    ("cache accepts a non-scope", [(CA, "        scope = require_scope(scope)\n        try:\n            blob", "        try:\n            blob")]),
    ("cache accepts non-JSON requests", [(CA, "        except (TypeError, ValueError):\n            raise InvalidRequestError", "        except ZeroDivisionError:\n            raise InvalidRequestError")]),
    ("cache ttl not validated", [(CA, " or not ttl_seconds > 0:", ":")]),
    ("cache max_entries not validated", [(CA, " or max_entries < 1:", ":")]),
    # --- resilience
    ("retry wait does not double", [(RE, "delay *= 2", "pass")]),
    ("retry wait ignores the cap", [(RE, "wait = min(delay, max_delay)", "wait = delay")]),
    ("retry does not stop after the last attempt", [(RE, "if i == attempts - 1:\n                raise", "if False:\n                raise")]),
    ("retry ignores the deadline", [(RE, "if deadline_seconds is not None and clock() - start + wait > deadline_seconds:", "if False:")]),
    ("retry deadline is off by one", [(RE, "clock() - start + wait > deadline_seconds", "clock() - start + wait >= deadline_seconds")]),
    ("retry retries every error", [(RE, "        except retry_on:\n            if i", "        except Exception:\n            if i")]),
    ("retry attempts not validated", [(RE, " or not 1 <= attempts <= 10:", ":")]),
    ("retry accepts non-Exception classes", [(RE, "issubclass(c, Exception)", "issubclass(c, BaseException)")]),
    ("retry delays not validated", [(RE, "        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:", "        if False:")]),
    ("retry deadline not validated", [(RE, " or deadline_seconds <= 0):", "):")]),
    ("fallback forgets the causes", [(RE, "                causes.append(e)", "                pass")]),
    ("fallback catches every error", [(RE, "            except retry_on as e:", "            except Exception as e:")]),
    ("all-failed message leaks the error text", [(RE, "', '.join(type(c).__name__ for c in causes)", "', '.join(str(c) for c in causes)")]),
    # --- evaluate
    ("judge true and false swapped", [(EV, "return 1.0 if s else 0.0", "return 0.0 if s else 1.0")]),
    ("judge range not checked", [(EV, " or not 0 <= s <= 1:", ":")]),
    ("a lower score is not a regression", [(EV, "if so < sb:", "if False:")]),
    ("an equal score is a regression", [(EV, "if so < sb:", "if so <= sb:")]),
    ("failures not counted", [(EV, "            kinds.append(type(e).__name__)\n", "            pass\n")]),
    ("small samples are summarised anyway", [(EV, "        if not self.enough:", "        if False:")]),
    ("enough is off by one", [(EV, "return self.n >= self.min_cases", "return self.n > self.min_cases")]),
    ("savings percentage wrong", [(EV, "saved = 100.0 * (1 - self.tokens_after / self.tokens_before)", "saved = 100.0 * (self.tokens_after / self.tokens_before)")]),
    ("bootstrap seed ignored", [(EV, "rng = random.Random(seed)", "rng = random.Random()")]),
    ("min_cases not validated", [(EV, "    if isinstance(min_cases, bool) or not isinstance(min_cases, int) or min_cases < 1:", "    if False:")]),
    ("bootstrap not validated", [(EV, "    if isinstance(bootstrap, bool) or not isinstance(bootstrap, int) or bootstrap < 1:", "    if False:")]),
    ("lossy cases not counted", [(EV, "lossy += 1 if result.report.lossy else 0", "lossy += 0")]),
    ("tokens after counted wrongly", [(EV, "ta += result.report.tokens_after", "ta += result.report.tokens_before")]),
    ("optimized answer taken from the original request", [(EV, "so = _score(judge, case, run(result.request))", 'so = _score(judge, case, run(case["request"]))')]),
    # --- model
    ("estimate uses 4 bytes per token", [(MO, 'len(text.encode("utf-8")) / 3', 'len(text.encode("utf-8")) / 4')]),
    ("list parts joined without a separator", [(MO, 'return "\\n".join(out)', 'return "".join(out)')]),
    ("tools not counted", [(MO, '    if "tools" in request:', "    if False:")]),
    ("system field not counted", [(MO, '    if isinstance(request.get("system"), str):', "    if False:")]),
    ("roles not counted", [(MO, ' + counter(str(m.get("role", "")))', "")]),
]


def run_tests(root: Path) -> bool:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
               PYTHONPATH=os.pathsep.join(p for p in (str(root / "src"), os.environ.get("PYTHONPATH", "")) if p))
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", str(root / "tests")],
                       cwd=root, env=env, capture_output=True, text=True)
    return r.returncode == 0


def main() -> int:
    survivors = []
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "base"
        shutil.copytree(PKG, base, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "scripts"))
        if not run_tests(base):
            print("The unmodified test suite fails; fix that first.")
            return 2
        print("baseline: tests pass")
        for i, (name, edits) in enumerate(MUTATIONS):
            work = Path(tmp) / f"m{i}"
            shutil.copytree(base, work)
            for rel, old, new in edits:
                p = work / rel
                text = p.read_text()
                if text.count(old) != 1:
                    print(f"ERROR  {name}: anchor found {text.count(old)} times (must be 1) in {rel}")
                    return 2
                p.write_text(text.replace(old, new))
            killed = not run_tests(work)
            print(("KILLED   " if killed else "SURVIVED ") + name)
            if not killed:
                survivors.append(name)
            shutil.rmtree(work)
    print(f"\n{len(MUTATIONS) - len(survivors)} of {len(MUTATIONS)} mutations killed")
    return 1 if survivors else 0


if __name__ == "__main__":
    sys.exit(main())
