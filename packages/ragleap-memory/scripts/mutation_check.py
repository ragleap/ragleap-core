#!/usr/bin/env python3
"""Mutation check for ragleap-memory (repository only, not part of the installed package).

For each mutation below it copies the package to a temp directory, breaks one protection in the
copy, and runs the test suite. A mutation is KILLED if a test fails, SURVIVED if every test still
passes (meaning that protection is not really tested). The real source is never modified.

  python scripts/mutation_check.py

Needs ragleap-security importable (installed, or on PYTHONPATH). Exit code 0 only if the unmodified
suite passes and every mutation is killed. Run from the package directory. A killed mutation shows a
protection is exercised by a test; it does not show the protection is sufficient."""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
S = "src/ragleap_memory/sqlite_store.py"
M = "src/ragleap_memory/model.py"
SH = "src/ragleap_memory/sharded.py"

# (name, [(file, old, new), ...]) ; each old string must occur exactly once in its file
MUTATIONS = [
    ("scope clause ignores the tenant", [(S, 'parts, params = [f"{p}tenant = ?"], [scope.tenant]', 'parts, params = ["1 = 1"], []')]),
    ("scope clause ignores narrower parts", [(S, "        if v is not None:\n            parts.append(", "        if False:\n            parts.append(")]),
    ("get checks ids but not the scope", [(S, 'row = db.execute(f"SELECT * FROM memories WHERE id = ? AND {clause}", [entry_id, *params]).fetchone()', 'row = db.execute("SELECT * FROM memories WHERE id = ?", [entry_id]).fetchone()')]),
    ("search drops the scope condition", [(S, 'f" WHERE memories_fts MATCH ? AND {clause}"', 'f" WHERE memories_fts MATCH ? AND 1 = 1"'), (S, "args: list = [match, *params, *kinds, *trusts]", "args: list = [match, *kinds, *trusts]")]),
    ("forget deletes by id only", [(S, 'n = db.execute(f"DELETE FROM memories WHERE id IN ({\',\'.join(\'?\' * len(ids))}) AND {clause}",\n                           [*ids, *params]).rowcount', 'n = db.execute(f"DELETE FROM memories WHERE id IN ({\',\'.join(\'?\' * len(ids))})",\n                           [*ids]).rowcount')]),
    ("erase deletes everything", [(S, 'n = db.execute(f"DELETE FROM memories WHERE {clause}", params).rowcount\n            if scope.subject is None', 'n = db.execute("DELETE FROM memories").rowcount\n            if scope.subject is None')]),
    ("sweep ignores the scope", [(S, 'n = db.execute(f"DELETE FROM memories WHERE {clause} AND expires_at IS NOT NULL AND expires_at <= ?",\n                           [*params, _iso(self._clock())]).rowcount', 'n = db.execute("DELETE FROM memories WHERE expires_at IS NOT NULL AND expires_at <= ?",\n                           [_iso(self._clock())]).rowcount')]),
    ("export ignores the scope", [(S, 'rows = db.execute(f"SELECT * FROM memories WHERE {clause} ORDER BY created_at, id", params).fetchall()', 'rows = db.execute("SELECT * FROM memories ORDER BY created_at, id").fetchall()')]),
    ("count ignores the scope", [(S, 'return self._db.execute(f"SELECT COUNT(*) FROM memories WHERE {clause}", params).fetchone()[0]', 'return self._db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]')]),
    ("derived_from not checked against the scope", [(S, "            for d in derived:\n                self._visible(db, scope, d)\n", "            for d in derived:\n                pass\n")]),
    ("supersede does not check the scope", [(S, "            old = self._visible(db, scope, old_id)\n", '            old = db.execute("SELECT * FROM memories WHERE id = ?", [old_id]).fetchone()\n            if old is None:\n                raise NotFoundError("no such entry")\n')]),
    ("double supersede allowed", [(S, 'if old["superseded_by"] is not None:', "if False:")]),
    ("default trust raised to owner", [(S, 'source: str = "api", trust: str = "untrusted",\n            importance: float = 0.5', 'source: str = "api", trust: str = "owner",\n            importance: float = 0.5')]),
    ("min_trust ignored", [(S, "trusts = [t for t in TRUSTS if trust_rank(t) >= trust_rank(min_trust)]", "trusts = list(TRUSTS)")]),
    ("expired entries not hidden", [(S, "        if not include_expired:\n            sql +=", "        if False:\n            sql +=")]),
    ("superseded entries not hidden", [(S, "        if not include_superseded:\n            sql +=", "        if False:\n            sql +=")]),
    ("query words not quoted", [(S, """match = " OR ".join('"%s"' % t for t in words)""", """match = " OR ".join(words)""")]),
    ("query size not limited", [(S, 'check_size(query, 1000, "query")', "pass")]),
    ("result cap removed", [(S, "k = min(k, self.max_results)", "pass")]),
    ("text size not limited", [(S, "check_size(text, self.max_text_bytes, \"text\")", "pass")]),
    ("metadata shape not limited", [(S, "check_json_shape(metadata, max_depth=8, max_nodes=1000)", "pass")]),
    ("audit written after the transaction (not rolled back)", [(S, "        if self._audit is not None:  # inside the transaction: if the audit write fails, the change is rolled back\n            self._audit.append(event, data, scope)", "        pass")]),
    ("secure_delete explicitly off", [(S, 'self._db.execute("PRAGMA secure_delete = ON")', 'self._db.execute("PRAGMA secure_delete = OFF")')]),
    ("erase does not compact", [(S, "            self._log(\"memory.erase\", scope, {\"count\": n})\n        self.compact()", "            self._log(\"memory.erase\", scope, {\"count\": n})")]),
    ("database file not private", [(S, "os.O_WRONLY | os.O_CREAT, 0o600", "os.O_WRONLY | os.O_CREAT, 0o644")]),
    ("newer schema accepted", [(S, "if version > SCHEMA_VERSION:", "if False:")]),
    ("failed write not rolled back", [(S, '                self._db.execute("ROLLBACK")\n                raise', "                raise")]),
    # --- 0.2.0: token budget, usage, quotas, migration, sharding ---
    ("token budget not enforced", [(S, 'if max_tokens is not None and used + r["tokens"] > max_tokens:\n                continue', 'if False:\n                continue')]),
    ("over-budget entry stops the walk instead of being skipped", [(S, 'used + r["tokens"] > max_tokens:\n                continue', 'used + r["tokens"] > max_tokens:\n                break')]),
    ("budgeted search looks at only k candidates", [(S, "args.append(self.max_results if max_tokens is not None else k)", "args.append(k)")]),
    ("k ignored under a budget", [(S, "            if len(hits) >= k:\n                break\n", "            if False:\n                break\n")]),
    ("served tokens not counted", [(S, "self._record_served(scope.tenant, used)", "self._record_served(scope.tenant, 0)")]),
    ("searches not counted", [(S, '" searches = searches + 1", [tenant, period, tokens])', '" searches = searches", [tenant, period, tokens])')]),
    ("entry quota ignored", [(S, "if self.quota_entries is not None and entries + 1 > self.quota_entries:", "if False:")]),
    ("token quota ignored", [(S, "if self.quota_tokens is not None and stored + tokens > self.quota_tokens:", "if False:")]),
    ("entry quota allows one extra", [(S, "entries + 1 > self.quota_entries:", "entries + 1 > self.quota_entries + 1:")]),
    ("token quota boundary off by one", [(S, "stored + tokens > self.quota_tokens:", "stored + tokens >= self.quota_tokens:")]),
    ("totals not reduced on delete", [(S, "SET entries = entries - 1, tokens = tokens - old.tokens WHERE", "SET entries = entries, tokens = tokens WHERE")]),
    ("estimate uses 4 bytes per token", [(M, 'len(text.encode("utf-8")) / 3', 'len(text.encode("utf-8")) / 4')]),
    ("estimator result not validated", [(S, "if isinstance(n, bool) or not isinstance(n, int) or n < 1:", "if False:")]),
    ("estimator failure leaks through", [(S, '        except Exception:\n            raise InvalidEntryError("token_estimator failed") from None', '        except ZeroDivisionError:\n            raise InvalidEntryError("token_estimator failed") from None')]),
    ("migration does not fill in tokens", [(S, 'db.executemany("UPDATE memories SET tokens = ? WHERE rowid = ?",\n                                   [(self._estimate(r["text"]), r["rowid"]) for r in rows])', "pass")]),
    ("migration does not rebuild totals", [(S, '" SELECT tenant, COUNT(*), SUM(tokens) FROM memories GROUP BY tenant")', '" SELECT tenant, COUNT(*), SUM(tokens) FROM memories WHERE 0 GROUP BY tenant")')]),
    ("failed migration is committed half way", [(S, '        except BaseException:\n            db.execute("ROLLBACK")\n            raise\n        db.execute("COMMIT")', '        except BaseException:\n            db.execute("COMMIT")\n            raise\n        db.execute("COMMIT")')]),
    ("old SQLite accepted", [(S, "if sqlite3.sqlite_version_info < (3, 24, 0):", "if False:")]),
    ("tenant-wide erase keeps usage counters", [(S, 'db.execute("DELETE FROM usage WHERE tenant = ?", [scope.tenant])', "pass")]),
    ("usage period format not validated", [(S, "if not isinstance(period, str) or not _PERIOD.match(period):", "if False:")]),
    ("usage period date not validated", [(S, '        try:\n            datetime.strptime(period, "%Y-%m-%d")\n        except ValueError:\n            raise InvalidEntryError("period must be a UTC day like 2026-10-31") from None\n', "")]),
    ("shard file named after the raw tenant id", [(SH, 'hashlib.sha256(tenant.encode("utf-8")).hexdigest()[:32]', "tenant")]),
    ("shard routing ignores the tenant", [(SH, "return os.path.join(self.directory, hashlib.sha256", 'return os.path.join(self.directory, "0" * 32); (hashlib.sha256')]),
    ("tenant-wide erase leaves files", [(SH, "os.remove(self._base(sc.tenant) + suffix)", "pass")]),
    ("narrow erase deletes the whole file", [(SH, "whole_tenant = scope.subject is None and scope.agent is None and scope.session is None", "whole_tenant = True")]),
    ("shards never evicted", [(SH, "if len(self._table) <= self.max_open:\n                    return", "if True:\n                    return")]),
    ("shard directory not private", [(SH, "os.chmod(self.directory, 0o700)", "os.chmod(self.directory, 0o755)")]),
    ("store options not passed to shards", [(SH, 'holder.store = SQLiteStore(self._base(scope.tenant) + ".db", **self._options)', 'holder.store = SQLiteStore(self._base(scope.tenant) + ".db")')]),
    ("evicted shard is not marked dead", [(SH, "                    holder.dead = True\n                    if holder.store is not None:\n                        holder.store.close()\n                        holder.store = None\n                    del self._table[tenant]", "                    if holder.store is not None:\n                        holder.store.close()\n                        holder.store = None\n                    del self._table[tenant]")]),
    # --- 0.2.0: download and upload ---
    ("import does not cap trust", [(S, "        if trust_rank(trust) > trust_rank(max_trust):\n            trust = max_trust", "        if False:\n            trust = max_trust")]),
    ("import sets every trust to max_trust", [(S, "if trust_rank(trust) > trust_rank(max_trust):", "if True:")]),
    ("import skips the scope check", [(S, '        if not target.covers(sc):\n            raise bad("entry is outside the import scope")', "        if False:\n            raise bad('x')")]),
    ("rescope ignored", [(S, "        if rescope:\n            sc = Scope(", "        if False:\n            sc = Scope(")]),
    ("duplicates not skipped", [(S, "                if skip_duplicates:\n                    existing = db.execute(", "                if False:\n                    existing = db.execute(")]),
    ("duplicate check ignores the subject", [(S, " AND subject IS ? AND agent IS ?", " AND (subject IS ? OR 1) AND agent IS ?")]),
    ("created_at not kept on import", [(S, 'created=it["created"]).id', "created=None).id")]),
    ("links keep file ids", [(S, "mapped = idmap.get(d)\n                    if mapped is None or", "mapped = idmap.get(d, d)\n                    if mapped is None or")]),
    ("derived links across scopes kept", [(S, " or not sc.covers(scope_of[mapped]):", ":")]),
    ("supersede links across scopes kept", [(S, " or scope_of[mapped] != sc:", ":")]),
    ("dropped links not counted", [(S, "mapped in derived or not sc.covers(scope_of[mapped]):\n                        dropped += 1", "mapped in derived or not sc.covers(scope_of[mapped]):\n                        pass")]),
    ("import not audited", [(S, 'self._log("memory.import", scope, {"imported": imported, "skipped": skipped})', "pass")]),
    ("header version not checked", [(S, 'or isinstance(obj.get("version"), bool) or obj.get("version") != EXPORT_VERSION', 'or isinstance(obj.get("version"), bool)')]),
    ("header version True accepted", [(S, 'or isinstance(obj.get("version"), bool) or obj.get("version") != EXPORT_VERSION', 'or obj.get("version") != EXPORT_VERSION')]),
    ("header format not checked", [(S, 'obj.get("format") != EXPORT_FORMAT', "False")]),
    ("entry count mismatch not detected", [(S, 'if len(out) != header["count"]:', "if False:")]),
    ("line size cap removed", [(S, "if len(raw) > MAX_LINE_BYTES:", "if False:")]),
    ("entry cap removed", [(S, "if len(out) >= max_entries:", "if False:")]),
    ("file size cap removed", [(S, "if total > max_bytes:", "if False:")]),
    ("unknown fields accepted", [(S, "if not keys <= _EXPORT_KEYS:", "if False:")]),
    ("required fields not checked", [(S, "if not _EXPORT_REQUIRED <= keys:", "if False:")]),
    ("file id not validated", [(S, 'if old_id is not None and (not isinstance(old_id, str) or not ID_RE.match(old_id)):', "if False:")]),
    ("derived_from size not capped", [(S, "or len(derived) > 100", "or False")]),
    ("max_trust not validated", [(S, '        if max_trust not in TRUSTS:\n            raise InvalidEntryError("invalid max_trust")', "        if False:\n            raise InvalidEntryError('x')")]),
    ("import size options not validated", [(S, '            if isinstance(v, bool) or not isinstance(v, int) or v < 1:\n                raise InvalidEntryError(f"{name} must be a positive int")', "            if False:\n                raise InvalidEntryError('x')")]),
    ("export file not private", [(S, "os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)", "os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)")]),
    ("export overwrites an existing file", [(S, "os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)", "os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)")]),
    ("failed export leaves a partial file", [(S, "                os.remove(os.fspath(path))", "                pass")]),
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
                    print(f"ERROR  {name}: anchor not found exactly once in {rel}")
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
