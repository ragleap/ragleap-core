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
from concurrent.futures import ThreadPoolExecutor
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
    ('expired entries not hidden', [(S, '        if not include_expired:\n            sql += " AND (m.expires_at IS NULL OR m.expires_at > ?)"\n            args.append(_iso(self._clock()))\n        sql += " ORDER BY score,', '        if False:\n            sql += " AND (m.expires_at IS NULL OR m.expires_at > ?)"\n            args.append(_iso(self._clock()))\n        sql += " ORDER BY score,')]),
    ('superseded entries not hidden', [(S, 'args: list = [match, *params, *kinds, *trusts]\n        if not include_superseded:', 'args: list = [match, *params, *kinds, *trusts]\n        if False:')]),
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
    ('served tokens not counted', [(S, '        if record:\n            self._record_served(scope.tenant, used)', '        if record:\n            self._record_served(scope.tenant, 0)')]),
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
    # --- 0.3.0: embeddings, vector and hybrid search ---
    ('0.3.0: model and size may be given separately', [(S, 'if (embedding_model is None) != (embedding_dim is None):', 'if False:')]),
    ('0.3.0: embedder accepted without model and size', [(S, 'if embedder is not None and embedding_model is None:', 'if False:')]),
    ('0.3.0: embedder not checked for being callable', [(S, 'if embedder is not None and not callable(embedder):', 'if False:')]),
    ('0.3.0: embedding_model not checked against the id pattern', [(S, 'if not isinstance(embedding_model, str) or not MODEL_RE.match(embedding_model):', 'if not isinstance(embedding_model, str):')]),
    ('0.3.0: embedding_dim range not enforced', [(S, 'or not 1 <= embedding_dim <= MAX_EMBEDDING_DIM):', 'or False):')]),
    ('0.3.0: bool accepted as embedding_dim', [(S, 'isinstance(embedding_dim, bool) or not isinstance(embedding_dim, int)', 'not isinstance(embedding_dim, int)')]),
    ('0.3.0: batch size and candidate cap not validated', [(S, 'if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= high:', 'if False:')]),
    ('0.3.0: batch size upper bound removed', [(S, '("embed_batch_size", embed_batch_size, 256)', '("embed_batch_size", embed_batch_size, 10**9)')]),
    ('0.3.0: embed_quota_tokens not validated', [(S, ',\n                        ("embed_quota_tokens", embed_quota_tokens)):', '):')]),
    ('0.3.0: no embedding config needed for vector features', [(S, '        if self.embedding_model is None:\n            raise InvalidEntryError("this store has no embedding_model and embedding_dim")', '        if False:\n            raise InvalidEntryError("x")')]),
    ('0.3.0: schema version not bumped', [(S, 'SCHEMA_VERSION = 3\n', 'SCHEMA_VERSION = 2\n')]),
    ('0.3.0: migration does not create the vector table', [(S, '            for ddl in _V3_DDL:\n                db.execute(ddl)', '            for ddl in ():\n                db.execute(ddl)')]),
    ('0.3.0: deleting an entry leaves its vector', [(S, '"CREATE TRIGGER IF NOT EXISTS embeddings_ad AFTER DELETE ON memories BEGIN"\n    " DELETE FROM embeddings WHERE rowid = old.rowid; END",', '"SELECT 1",')]),
    ('0.3.0: migration does not add the usage counters', [(S, '                if col not in usage_cols:', '                if False:')]),
    ('0.3.0: embed_pending ignores the scope', [(S, 'f"FROM memories m LEFT JOIN embeddings e ON e.rowid = m.rowid WHERE {clause}"', 'f"FROM memories m LEFT JOIN embeddings e ON e.rowid = m.rowid WHERE 1 = 1"')]),
    ('0.3.0: superseded entries are embedded', [(S, 'f" AND m.superseded_by IS NULL AND (m.expires_at IS NULL OR m.expires_at > ?)"', 'f" AND (m.expires_at IS NULL OR m.expires_at > ?)"')]),
    ('0.3.0: expired entries are embedded', [(S, 'f" AND m.superseded_by IS NULL AND (m.expires_at IS NULL OR m.expires_at > ?)"', 'f" AND m.superseded_by IS NULL AND (m.expires_at IS NULL OR m.expires_at > ? OR 1 = 1)"')]),
    ('0.3.0: entries that have a vector are embedded again', [(S, 'f" AND (e.rowid IS NULL{stale})")', 'f" AND (1 = 1{stale})")')]),
    ('0.3.0: replace_stale misses a different size', [(S, '" OR e.model != ? OR e.dim != ?"', '" OR e.model != ? OR 0 = ?"')]),
    ('0.3.0: replace_stale misses a different model', [(S, '" OR e.model != ? OR e.dim != ?"', '" OR 0 = ? OR e.dim != ?"')]),
    ('0.3.0: embed_pending takes the oldest entries first', [(S, '+ " ORDER BY m.created_at DESC, m.rowid DESC LIMIT ?", [*args, limit]).fetchall()', '+ " ORDER BY m.created_at ASC, m.rowid ASC LIMIT ?", [*args, limit]).fetchall()')]),
    ('0.3.0: embed_pending ignores limit', [(S, '[*args, limit]).fetchall()', '[*args, 1000]).fetchall()')]),
    ('0.3.0: daily cap does not trim the batch', [(S, '                if total + r["tokens"] > room:\n                    break', '                if False:\n                    break')]),
    ('0.3.0: daily cap refuses exactly at the cap', [(S, '                if total + r["tokens"] > room:', '                if total + r["tokens"] >= room:')]),
    ('0.3.0: nothing fits the cap but no error', [(S, 'if chosen and not fits and not dry_run:', 'if False:')]),
    ('0.3.0: dry run raises on the cap', [(S, 'if chosen and not fits and not dry_run:', 'if chosen and not fits:')]),
    ('0.3.0: dry run still embeds', [(S, '        if dry_run:\n            return EmbedResult(', '        if False:\n            return EmbedResult(')]),
    ('0.3.0: dry run miscounts the calls', [(S, 'batches = -(-len(chosen) // self.embed_batch_size)', 'batches = len(chosen) // self.embed_batch_size')]),
    ('0.3.0: batch size ignored', [(S, 'for i in range(0, len(chosen), self.embed_batch_size):', 'for i in range(0, len(chosen), 1000):')]),
    ('0.3.0: embedder tokens never reserved', [(S, '            self._reserve_embed(scope.tenant, batch_tokens)\n            spent += batch_tokens', '            spent += batch_tokens')]),
    ('0.3.0: batch tokens counted as zero', [(S, 'batch_tokens = sum(r["tokens"] for r in batch)', 'batch_tokens = 0')]),
    ('0.3.0: vector stored for an entry that is gone', [(S, '" WHERE EXISTS (SELECT 1 FROM memories WHERE rowid = ?)"', '" WHERE ? IS NOT NULL"')]),
    ('0.3.0: embedding not audited', [(S, '                self._log("memory.embed", scope, {"count": stored, "tokens": batch_tokens})', '                pass')]),
    ('0.3.0: embed_pending limit range not enforced', [(S, 'if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:', 'if isinstance(limit, bool) or not isinstance(limit, int):')]),
    ('0.3.0: dry_run and replace_stale not validated', [(S, 'if not isinstance(dry_run, bool) or not isinstance(replace_stale, bool):', 'if False:')]),
    ('0.3.0: embed_pending without an embedder', [(S, 'if self._embedder is None and not dry_run:', 'if False:')]),
    ('0.3.0: vectors stored without normalizing', [(S, 'return array("f", (v / norm for v in values))', 'return array("f", values)')]),
    ('0.3.0: all-zero vector accepted', [(S, 'if not math.isfinite(norm) or norm == 0.0:', 'if not math.isfinite(norm):')]),
    ('0.3.0: bool accepted in a vector', [(S, 'if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):', 'if not isinstance(x, (int, float)) or not math.isfinite(x):')]),
    ('0.3.0: text accepted in a vector', [(S, 'if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):', 'if isinstance(x, bool) or not math.isfinite(float(x)):')]),
    ('0.3.0: wrong vector size accepted', [(S, '    if n != dim:\n', '    if False:\n')]),
    ('0.3.0: wrong number of vectors accepted', [(S, '            if len(out) != len(texts):', '            if False:')]),
    ('0.3.0: embedder error passes its own message through', [(S, '        except Exception:\n            raise EmbedderError("the embedder failed") from None', '        except Exception:\n            raise')]),
    ('0.3.0: unusable embedder result raises a bare TypeError', [(S, '        except (InvalidEntryError, TypeError):\n            raise EmbedderError', '        except InvalidEntryError:\n            raise EmbedderError')]),
    ('0.3.0: query embedding not checked against the cap', [(S, 'if (row["embedded_tokens"] if row else 0) + tokens > self.embed_quota_tokens:', 'if False:')]),
    ('0.3.0: cap refuses exactly at the cap when reserving', [(S, 'if (row["embedded_tokens"] if row else 0) + tokens > self.embed_quota_tokens:', 'if (row["embedded_tokens"] if row else 0) + tokens >= self.embed_quota_tokens:')]),
    ('0.3.0: embedder calls not counted', [(S, '                       " embed_calls = embed_calls + 1", [tenant, period, tokens])', '                       " embed_calls = embed_calls", [tenant, period, tokens])')]),
    ('0.3.0: embedder tokens not counted', [(S, '" embedded_tokens = embedded_tokens + excluded.embedded_tokens,"', '" embedded_tokens = embedded_tokens,"')]),
    ('0.3.0: the cap never resets (fixed day)', [(S, '        period = self._period(None)\n        with self._tx() as db:\n            if self.embed_quota_tokens is not None:', '        period = "2000-01-01"\n        with self._tx() as db:\n            if self.embed_quota_tokens is not None:')]),
    ('0.3.0: usage hides embedded_tokens', [(S, '"embedded_tokens": row["embedded_tokens"] if row else 0,', '"embedded_tokens": 0,')]),
    ('0.3.0: usage hides embed_calls', [(S, '"embed_calls": row["embed_calls"] if row else 0}', '"embed_calls": 0}')]),
    ('0.3.0: any search mode accepted', [(S, '        if mode not in SEARCH_MODES:', '        if False:')]),
    ('0.3.0: query_vector accepted in keyword mode', [(S, '            if query_vector is not None:\n                raise InvalidEntryError("query_vector needs mode \'vector\' or \'hybrid\'")', "            if False:\n                raise InvalidEntryError('x')")]),
    ('0.3.0: vector query size not limited', [(S, 'check_size(query, MAX_QUERY_BYTES, "query")', 'pass')]),
    ('0.3.0: vector query type not checked', [(S, '        if not isinstance(query, str):\n            raise InvalidEntryError("query must be a string")\n        check_size(query, MAX_QUERY_BYTES', '        if False:\n            raise InvalidEntryError("x")\n        check_size(query, MAX_QUERY_BYTES')]),
    ('0.3.0: vector k not validated', [(S, '        check_size(query, MAX_QUERY_BYTES, "query")\n        if isinstance(k, bool) or not isinstance(k, int) or k < 1:', '        check_size(query, MAX_QUERY_BYTES, "query")\n        if False:')]),
    ('0.3.0: vector max_tokens not validated', [(S, '        top = min(k, self.max_results)\n        if max_tokens is not None and (', '        top = min(k, self.max_results)\n        if False and (')]),
    ('0.3.0: vector min_trust not validated', [(S, '        if min_trust not in TRUSTS:\n            raise InvalidEntryError("invalid min_trust")\n        wanted_kinds', '        if False:\n            raise InvalidEntryError("x")\n        wanted_kinds')]),
    ('0.3.0: vector kinds not validated', [(S, '        if not wanted_kinds or any(x not in KINDS for x in wanted_kinds):', '        if False:')]),
    ('0.3.0: vector search ignores the kinds filter', [(S, '        wanted_kinds = tuple(kinds) if kinds is not None else KINDS', '        wanted_kinds = KINDS')]),
    ('0.3.0: vector search ignores min_trust', [(S, '        trusts = [t for t in TRUSTS if trust_rank(t) >= floor]', '        trusts = list(TRUSTS)')]),
    ('0.3.0: vector search drops the scope', [(S, '        clause, params = _scope_clause(scope, "m")\n        sql = ("SELECT m.*, e.vec AS vec', '        clause, params = "1 = 1", []\n        sql = ("SELECT m.*, e.vec AS vec')]),
    ('0.3.0: vector search ignores the model', [(S, 'f" WHERE e.model = ? AND e.dim = ? AND {clause}"', 'f" WHERE (e.model = ? OR 1 = 1) AND e.dim = ? AND {clause}"')]),
    ('0.3.0: vector search shows superseded entries', [(S, '        args: list = [self.embedding_model, self.embedding_dim, *params, *wanted_kinds, *trusts]\n        if not include_superseded:', '        args: list = [self.embedding_model, self.embedding_dim, *params, *wanted_kinds, *trusts]\n        if False:')]),
    ('0.3.0: vector search shows expired entries', [(S, '        if not include_expired:\n            sql += " AND (m.expires_at IS NULL OR m.expires_at > ?)"\n            args.append(_iso(self._clock()))\n        sql += " ORDER BY m.created_at DESC, m.rowid DESC LIMIT ?"', '        if False:\n            sql += " AND (m.expires_at IS NULL OR m.expires_at > ?)"\n            args.append(_iso(self._clock()))\n        sql += " ORDER BY m.created_at DESC, m.rowid DESC LIMIT ?"')]),
    ('0.3.0: vector candidate cap ignored', [(S, '        args.append(self.max_vector_candidates)', '        args.append(100000)')]),
    ('0.3.0: vector scan takes the oldest entries first', [(S, '        sql += " ORDER BY m.created_at DESC, m.rowid DESC LIMIT ?"\n        args.append(self.max_vector_candidates)', '        sql += " ORDER BY m.created_at ASC, m.rowid ASC LIMIT ?"\n        args.append(self.max_vector_candidates)')]),
    ('0.3.0: damaged vectors not skipped', [(S, '            if len(r["vec"]) != 4 * self.embedding_dim:\n                continue', '            if False:\n                continue')]),
    ('0.3.0: vector results not ordered by similarity', [(S, '        scored.sort(key=lambda s: -s[0])', '        scored.sort(key=lambda s: s[0])')]),
    ('0.3.0: equal scores not newest first', [(S, '        scored.sort(key=lambda s: -s[0])', '        scored.reverse()\n        scored.sort(key=lambda s: -s[0])')]),
    ('0.3.0: similarity does not use the stored vector', [(S, 'sum(map(mul, qvec, _from_blob(r["vec"])))', 'sum(map(mul, qvec, qvec))')]),
    ('0.3.0: blank vector query not short-circuited', [(S, '        elif not query.strip():\n            self._record_served(scope.tenant, 0)\n            return []', '        elif False:\n            self._record_served(scope.tenant, 0)\n            return []')]),
    ('0.3.0: blank vector query not counted as a search', [(S, '        elif not query.strip():\n            self._record_served(scope.tenant, 0)', '        elif not query.strip():\n            pass')]),
    ('0.3.0: vector search without embedder or query_vector', [(S, '        elif self._embedder is None:\n            raise InvalidEntryError("vector search needs an embedder or a query_vector")', "        elif False:\n            raise InvalidEntryError('x')")]),
    ('0.3.0: query embedding not counted', [(S, '            self._reserve_embed(scope.tenant, self._estimate(query))\n', '            pass\n')]),
    ('0.3.0: query_vector not validated', [(S, '            qvec = _unit_vector(query_vector, self.embedding_dim)', "            qvec = array('f', query_vector)")]),
    ('0.3.0: vector token budget not enforced', [(S, '            if max_tokens is not None and used + tokens > max_tokens:\n                continue\n            used += tokens\n            hits.append(SearchHit(entry,', '            if False:\n                continue\n            used += tokens\n            hits.append(SearchHit(entry,')]),
    ('0.3.0: vector k not enforced', [(S, '            if len(hits) >= top:\n                break', '            if False:\n                break')]),
    ('0.3.0: vector k not capped at max_results', [(S, '        top = min(k, self.max_results)', '        top = k')]),
    ('0.3.0: vector served tokens not counted', [(S, '            hits.append(SearchHit(entry, float(score), tokens))\n        self._record_served(scope.tenant, used)', '            hits.append(SearchHit(entry, float(score), tokens))\n        self._record_served(scope.tenant, 0)')]),
    ('0.3.0: hybrid keyword part is cut to one result', [(S, '            keyword = self._keyword_search(scope, query, k=self.max_results,', '            keyword = self._keyword_search(scope, query, k=1,')]),
    ('0.3.0: hybrid counts the search twice', [(S, '                                           max_tokens=None, record=False)', '                                           max_tokens=None, record=True)')]),
    ('0.3.0: hybrid keyword part shows expired entries', [(S, '                                           include_superseded=include_superseded, include_expired=include_expired,\n                                           max_tokens=None, record=False)', '                                           include_superseded=include_superseded, include_expired=True,\n                                           max_tokens=None, record=False)')]),
    ('0.3.0: hybrid keyword part shows superseded entries', [(S, '                                           include_superseded=include_superseded, include_expired=include_expired,\n                                           max_tokens=None, record=False)', '                                           include_superseded=True, include_expired=include_expired,\n                                           max_tokens=None, record=False)')]),
    ('0.3.0: hybrid keyword part ignores the kinds filter', [(S, '            keyword = self._keyword_search(scope, query, k=self.max_results, kinds=kinds,', '            keyword = self._keyword_search(scope, query, k=self.max_results, kinds=None,')]),
    ('0.3.0: hybrid keyword part ignores min_trust', [(S, '            keyword = self._keyword_search(scope, query, k=self.max_results, kinds=kinds, min_trust=min_trust,', '            keyword = self._keyword_search(scope, query, k=self.max_results, kinds=kinds, min_trust="untrusted",')]),
    ('0.3.0: hybrid keyword rank not used', [(S, '                fused[h.entry.id] = [1.0 / (RRF_K + rank), h.entry, h.tokens]', '                fused[h.entry.id] = [0.0, h.entry, h.tokens]')]),
    ('0.3.0: hybrid vector rank not added to a keyword hit', [(S, '                    fused[entry.id][0] += 1.0 / (RRF_K + rank)', '                    fused[entry.id][0] += 0')]),
    ('0.3.0: hybrid vector-only hits get no score', [(S, '                    fused[entry.id] = [1.0 / (RRF_K + rank), entry, tokens]', '                    fused[entry.id] = [0.0, entry, tokens]')]),
    ('0.3.0: reciprocal rank constant changed', [(S, 'RRF_K = 60\n', 'RRF_K = 1\n')]),
    ('0.3.0: hybrid results not ordered by fused score', [(S, '            merged.sort(key=lambda f: -f[0])', '            merged.sort(key=lambda f: f[0])')]),
    ('0.3.0: sharded embed_pending goes to the wrong shard', [(SH, 'return self._run(scope, lambda s, sc: s.embed_pending(sc, **kw))', 'return self._run(Scope("zzz-other"), lambda s, sc: s.embed_pending(sc, **kw))')]),
    ('0.3.0: sharded embed_pending drops its options', [(SH, 'lambda s, sc: s.embed_pending(sc, **kw)', 'lambda s, sc: s.embed_pending(sc)')]),
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
        shutil.copytree(PKG, base, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "scripts"))
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
