import json
import sqlite3
from datetime import timedelta

import pytest

from ragleap_memory import InvalidEntryError, MemoryStoreError, QuotaExceededError, SQLiteStore
from ragleap_security import AuditLog, Scope, verify

A = Scope("acme")
A_ALICE = Scope("acme", subject="alice")
B = Scope("globex")

V1_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories(
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    tenant TEXT NOT NULL, subject TEXT, agent TEXT, session TEXT,
    kind TEXT NOT NULL CHECK(kind IN ('episode','fact','summary')),
    text TEXT NOT NULL,
    source TEXT NOT NULL,
    trust TEXT NOT NULL CHECK(trust IN ('untrusted','model','owner')),
    importance REAL NOT NULL CHECK(importance >= 0 AND importance <= 1),
    created_at TEXT NOT NULL,
    expires_at TEXT,
    superseded_by TEXT,
    derived_from TEXT NOT NULL,
    metadata TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS memories_scope ON memories(tenant, subject, agent, session);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    text, content='memories', content_rowid='rowid', tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
END;
"""


def totals_match(s: SQLiteStore) -> bool:
    """The maintained totals equal a fresh count of the rows."""
    real = {r[0]: (r[1], r[2]) for r in s._db.execute(
        "SELECT tenant, COUNT(*), COALESCE(SUM(tokens), 0) FROM memories GROUP BY tenant")}
    kept = {r[0]: (r[1], r[2]) for r in s._db.execute("SELECT tenant, entries, tokens FROM tenant_totals")
            if r[1] or r[2]}
    return real == kept


def test_usage_per_scope(store):
    store.add(A, "abcdefghi")                  # tenant level, 3 tokens
    store.add(A_ALICE, "abcdef")               # alice, 2 tokens
    store.add(B, "abcdefghijkl")               # another tenant, 4 tokens
    u = store.usage(A)
    assert (u["entries"], u["stored_tokens"]) == (2, 5)
    u = store.usage(A_ALICE)
    assert (u["entries"], u["stored_tokens"]) == (1, 2)  # a narrow scope sees only its own entries
    assert store.usage(B)["stored_tokens"] == 4


def test_usage_follows_deletes(store):
    e = store.add(A, "abcdefghi")
    store.add(A, "abcdef")
    assert store.usage(A)["entries"] == 2
    store.forget(A, [e.id])
    u = store.usage(A)
    assert (u["entries"], u["stored_tokens"]) == (1, 2)
    assert totals_match(store)


def test_served_tokens_per_tenant_and_day(clock):
    s = SQLiteStore(clock=clock)
    s.add(A, "common abcdefghi")   # 16 bytes -> 6 tokens
    s.add(B, "common other")
    served = sum(h.tokens for h in s.search(A, "common"))
    s.search(A, "common")
    u = s.usage(A)
    assert u["period"] == "2026-10-31"
    assert (u["served_tokens"], u["searches"]) == (2 * served, 2)
    assert s.usage(B)["searches"] == 0          # counters are per tenant
    assert s.usage(A, "2026-10-30")["searches"] == 0
    clock.advance(days=1)
    s.search(A, "common")
    assert s.usage(A)["searches"] == 1 and s.usage(A)["period"] == "2026-11-01"
    assert s.usage(A, "2026-10-31")["searches"] == 2   # yesterday is kept


def test_a_search_with_no_results_is_still_counted(store):
    store.search(A, "nothing matches this")
    store.search(A, "!!!")                      # no words at all
    u = store.usage(A)
    assert (u["searches"], u["served_tokens"]) == (2, 0)


@pytest.mark.parametrize("bad", ["2026-13-01", "2026-1-1", "tomorrow", "2026-10-31\n", "2026-02-30", 20261031, ""])
def test_bad_period_refused(store, bad):
    with pytest.raises(InvalidEntryError):
        store.usage(A, bad)


def test_tenant_wide_erase_forgets_counters_but_narrow_erase_does_not(store):
    store.add(A, "common one")
    store.add(A_ALICE, "common two")
    store.search(A, "common")
    store.erase(A_ALICE)
    assert store.usage(A)["searches"] == 1
    store.erase(A)
    assert store.usage(A)["searches"] == 0
    store.add(B, "common x")
    store.search(B, "common")
    store.erase(A)
    assert store.usage(B)["searches"] == 1      # another tenant's counters stay


def test_entry_quota():
    s = SQLiteStore(quota_entries=3)
    for i in range(3):
        s.add(A, f"entry {i}")                   # exactly at the quota is allowed
    with pytest.raises(QuotaExceededError):
        s.add(A, "one too many")
    assert s.count(A) == 3
    s.add(B, "other tenant is unaffected")


def test_token_quota_and_its_boundary():
    s = SQLiteStore(quota_tokens=10)
    for _ in range(3):
        s.add(A, "abcdefghi")                    # 3 tokens each: 9 stored
    with pytest.raises(QuotaExceededError):
        s.add(A, "abcdefghi")                    # would make 12
    s.add(A, "a")                                # 1 token: exactly 10 is allowed
    with pytest.raises(QuotaExceededError):
        s.add(A, "b")                            # 11
    assert s.usage(A)["stored_tokens"] == 10


def test_deleting_frees_quota():
    s = SQLiteStore(quota_entries=1, quota_tokens=5)
    e = s.add(A, "abcdefghi")
    with pytest.raises(QuotaExceededError):
        s.add(A, "abc")
    s.forget(A, [e.id])
    s.add(A, "abc")


def test_supersede_needs_a_free_slot_because_history_is_kept():
    s = SQLiteStore(quota_entries=2)
    old = s.add(A, "first version")
    s.add(A, "something else")
    with pytest.raises(QuotaExceededError):
        s.supersede(A, old.id, "second version")
    assert s.get(A, old.id).superseded_by is None   # nothing was changed
    s2 = SQLiteStore(quota_entries=3)
    old = s2.add(A, "first version")
    s2.supersede(A, old.id, "second version")
    assert s2.count(A) == 2


def test_refused_write_leaves_nothing_and_is_not_audited(tmp_path):
    log = AuditLog(str(tmp_path / "audit.jsonl"))
    s = SQLiteStore(audit=log, quota_entries=1)
    s.add(A, "kept")
    before = verify(str(tmp_path / "audit.jsonl")).records
    with pytest.raises(QuotaExceededError) as ei:
        s.add(A, "private refused words")
    assert "private" not in str(ei.value)
    assert verify(str(tmp_path / "audit.jsonl")).records == before
    assert s.count(A) == 1 and totals_match(s)


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "5"])
@pytest.mark.parametrize("name", ["quota_entries", "quota_tokens"])
def test_bad_quota_settings_refused(name, bad):
    with pytest.raises(InvalidEntryError):
        SQLiteStore(**{name: bad})


def test_totals_stay_equal_to_the_rows(store):
    a = store.add(A, "abcdefghi")
    store.add(A_ALICE, "abcdef")
    store.add(B, "abcdefghijkl")
    store.supersede(A, a.id, "new text here")
    store.forget(B, [e["id"] for e in store.export(B)])
    store.erase(A_ALICE)
    assert totals_match(store)


def make_v1_db(path):
    db = sqlite3.connect(path)
    db.executescript(V1_SCHEMA)
    rows = [("a" * 32, "acme", "first note"), ("b" * 32, "acme", "second note here"), ("c" * 32, "globex", "other note")]
    for i, (eid, tenant, text) in enumerate(rows):
        db.execute("INSERT INTO memories(id, tenant, subject, agent, session, kind, text, source, trust, importance,"
                   " created_at, expires_at, superseded_by, derived_from, metadata)"
                   " VALUES (?,?,NULL,NULL,NULL,'fact',?,'api','owner',0.5,'2026-10-01T00:00:00.000000+00:00',NULL,NULL,?,?)",
                   [eid, tenant, text, json.dumps([]), json.dumps({})])
    db.execute("PRAGMA user_version = 1")
    db.commit()
    db.close()


def test_a_010_database_migrates_and_its_totals_feed_the_quota(tmp_path):
    p = str(tmp_path / "old.db")
    make_v1_db(p)
    s = SQLiteStore(p, quota_entries=2)
    assert s.usage(A)["entries"] == 2
    assert s.get(A, "a" * 32).tokens == 4       # filled in for rows written by 0.1.0
    assert [h.entry.id for h in s.search(A, "second")] == ["b" * 32]   # search still works
    with pytest.raises(QuotaExceededError):      # the rebuilt totals count the old rows
        s.add(A, "a third entry")
    s.add(B, "globex has room")
    assert totals_match(s)
    assert s._db.execute("PRAGMA user_version").fetchone()[0] == 3
    s.close()
    s = SQLiteStore(p)                           # a second open migrates nothing again
    assert s.count(A) == 2 and totals_match(s)


def test_a_failed_migration_leaves_the_old_file_untouched(tmp_path):
    p = str(tmp_path / "old.db")
    make_v1_db(p)
    calls = []

    def flaky(text):
        calls.append(text)
        if len(calls) == 2:
            raise RuntimeError("stop half way")
        return 1
    with pytest.raises(InvalidEntryError):
        SQLiteStore(p, token_estimator=flaky)
    db = sqlite3.connect(p)
    assert db.execute("PRAGMA user_version").fetchone()[0] == 1
    assert "tokens" not in [r[1] for r in db.execute("PRAGMA table_info(memories)")]
    assert db.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 3
    db.close()
    assert SQLiteStore(p).count(A) == 2          # and the next open still migrates


def test_old_sqlite_is_refused(monkeypatch):
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 23, 1))
    with pytest.raises(MemoryStoreError):
        SQLiteStore()
