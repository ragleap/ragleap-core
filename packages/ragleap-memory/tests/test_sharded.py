import os
import stat
import threading
from datetime import datetime, timezone

import pytest

from ragleap_memory import InvalidEntryError, NotFoundError, QuotaExceededError, ShardedStore
from ragleap_security import AuditLog, Scope, ScopeError, verify

A = Scope("acme")
A_ALICE = Scope("acme", subject="alice")
B = Scope("globex")


def mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def all_bytes(directory) -> bytes:
    return b"".join(open(os.path.join(directory, f), "rb").read() for f in sorted(os.listdir(directory)))


@pytest.fixture
def sharded(tmp_path):
    s = ShardedStore(tmp_path / "mem")
    yield s
    s.close()


def test_one_file_per_tenant_named_by_hash(sharded, tmp_path):
    sharded.add(A, "x y")
    sharded.add(B, "x y")
    names = [n for n in os.listdir(tmp_path / "mem") if n.endswith(".db")]
    assert len(names) == 2
    for n in names:
        stem = n[:-3]
        assert len(stem) == 32 and all(c in "0123456789abcdef" for c in stem)
    assert not any("acme" in n or "globex" in n for n in os.listdir(tmp_path / "mem"))


def test_directory_and_files_are_private(tmp_path):
    d = tmp_path / "mem"
    d.mkdir(mode=0o755)
    os.chmod(d, 0o755)
    s = ShardedStore(d)
    s.add(A, "x y")
    assert mode(d) == 0o700
    for n in os.listdir(d):
        assert mode(d / n) == 0o600, n
    s.close()


def test_tenants_are_isolated_like_in_the_single_file_store(sharded):
    e = sharded.add(A, "secret alpha beta")
    with pytest.raises(NotFoundError):
        sharded.get(B, e.id)
    assert sharded.search(B, "secret") == []
    assert sharded.count(B) == 0 and sharded.export(B) == []
    assert sharded.forget(B, [e.id]) == 0
    assert sharded.count(A) == 1


def test_narrow_scopes_work_inside_a_shard(sharded):
    sharded.add(A, "tenant level note")
    mine = sharded.add(A_ALICE, "alice note")
    assert [h.entry.id for h in sharded.search(A_ALICE, "note")] == [mine.id]
    assert sharded.count(A) == 2


def test_tenant_wide_erase_deletes_the_files_and_the_text_is_gone(tmp_path):
    d = tmp_path / "mem"
    s = ShardedStore(d)
    s.add(A, "zebra-marker-text for acme")
    s.add(B, "keep this globex entry")
    assert b"zebra-marker-text" in all_bytes(d)      # control: the text is in the files before the erase
    assert s.erase(A) == 1
    assert b"zebra-marker-text" not in all_bytes(d)
    assert len([n for n in os.listdir(d) if n.endswith(".db")]) == 1   # only globex is left
    assert not any(n.endswith(("-wal", "-shm")) and os.path.exists(d / n) and "acme" in n for n in os.listdir(d))
    assert s.count(B) == 1
    s.add(A, "a fresh start")                         # the tenant can start again
    assert s.count(A) == 1
    s.close()


def test_tenant_wide_erase_leaves_no_journal_files_of_that_tenant(tmp_path):
    d = tmp_path / "mem"
    s = ShardedStore(d)
    s.add(A, "x y")
    stem = [n for n in os.listdir(d) if n.endswith(".db")][0][:-3]
    s.erase(A)
    assert [n for n in os.listdir(d) if n.startswith(stem)] == []
    s.close()


def test_narrow_erase_keeps_the_file_and_the_rest(sharded, tmp_path):
    sharded.add(A, "tenant note")
    sharded.add(A_ALICE, "alice note")
    assert sharded.erase(A_ALICE) == 1
    assert len([n for n in os.listdir(tmp_path / "mem") if n.endswith(".db")]) == 1
    assert sharded.count(A) == 1


def test_quotas_apply_per_tenant(tmp_path):
    s = ShardedStore(tmp_path / "mem", quota_entries=1)
    s.add(A, "one")
    with pytest.raises(QuotaExceededError):
        s.add(A, "two")
    s.add(B, "one for globex")
    s.close()


def test_usage_and_budget_work_through_shards(sharded):
    for i in range(20):
        sharded.add(A, f"common words number {i}")
    hits = sharded.search(A, "common", k=20, max_tokens=30)
    served = sum(h.tokens for h in hits)
    assert 0 < served <= 30
    u = sharded.usage(A)
    assert (u["entries"], u["searches"], u["served_tokens"]) == (20, 1, served)
    assert sharded.usage(B)["searches"] == 0


def test_at_most_max_open_shards_stay_open_and_data_survives(tmp_path):
    s = ShardedStore(tmp_path / "mem", max_open=2)
    tenants = [Scope(f"t{i}") for i in range(5)]
    for t in tenants:
        s.add(t, f"entry of {t.tenant}")
        assert len(s._table) <= 2
        assert sum(1 for h in s._table.values() if h.store is not None) <= 2
    for t in tenants:                                 # evicted shards reopen on demand
        assert s.count(t) == 1
        assert [h.entry.text for h in s.search(t, "entry")] == [f"entry of {t.tenant}"]
    s.close()


def test_one_audit_log_sees_every_tenant(tmp_path):
    p = str(tmp_path / "audit.jsonl")
    s = ShardedStore(tmp_path / "mem", audit=AuditLog(p))
    s.add(A, "x y")
    s.add(B, "x y")
    r = verify(p)
    assert r.ok and r.records == 2
    s.close()


def test_threads_across_tenants_with_eviction(tmp_path):
    s = ShardedStore(tmp_path / "mem", max_open=2)
    tenants = [Scope("t0"), Scope("t1"), Scope("t2")]
    errors = []

    def work(i):
        try:
            for n in range(25):
                s.add(tenants[i % 3], f"thread {i} entry {n}")
        except Exception as e:  # pragma: no cover - only on failure
            errors.append(e)
    threads = [threading.Thread(target=work, args=(i,)) for i in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    assert [s.count(t) for t in tenants] == [50, 50, 50]
    s.close()


def test_tenant_ids_cannot_escape_the_directory(tmp_path):
    for bad in ("../x", "a/b", "..", "a\\b"):
        with pytest.raises(ScopeError):
            Scope(bad)
    s = ShardedStore(tmp_path / "mem")
    s.add(Scope("a.b"), "x y")
    assert all(os.path.dirname(os.path.join(tmp_path / "mem", n)) == str(tmp_path / "mem")
               for n in os.listdir(tmp_path / "mem"))
    s.close()


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "2", None])
def test_bad_max_open_refused(tmp_path, bad):
    with pytest.raises(InvalidEntryError):
        ShardedStore(tmp_path / "mem", max_open=bad)


def test_bad_store_options_are_refused_at_construction(tmp_path):
    with pytest.raises(InvalidEntryError):
        ShardedStore(tmp_path / "mem", quota_entries=0)
    with pytest.raises(TypeError):
        ShardedStore(tmp_path / "mem", nonsense=1)


def test_store_options_reach_every_shard(tmp_path):
    fixed = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    s = ShardedStore(tmp_path / "mem", clock=lambda: fixed, token_estimator=lambda t: 7, max_results=2)
    for scope in (A, B):
        for i in range(5):
            e = s.add(scope, f"common {i}")
        assert e.tokens == 7 and e.created_at.startswith("2026-01-02T03:04:05")
        assert len(s.search(scope, "common", k=50)) == 2
    s.close()


def test_only_real_scopes_are_accepted(sharded):
    with pytest.raises(ScopeError):
        sharded.add("acme", "x y")


def test_compact_and_reopen(tmp_path):
    d = tmp_path / "mem"
    s = ShardedStore(d)
    e = s.add(A, "persist me")
    s.compact(A)
    s.close()
    s2 = ShardedStore(d)
    assert s2.get(A, e.id).text == "persist me"
    s2.close()


def test_a_shard_evicted_between_lookup_and_use_is_looked_up_again(tmp_path):
    """The race: a thread has found a shard's holder, and before it uses it another thread evicts it.
    The first thread must notice and look the tenant up again, not open a second copy outside the table."""
    s = ShardedStore(tmp_path / "mem")
    real = s._table_lock
    fired = []

    class Hook:
        def __enter__(self):
            real.acquire()
            return self

        def __exit__(self, *exc):
            real.release()
            if not fired and s._table:     # right after the first lookup released the table lock
                fired.append(1)
                s.max_open = 0
                s._evict()                  # evicts the holder the first thread already holds
                s.max_open = 64

    s._table_lock = Hook()
    s.add(A, "x y")
    assert fired == [1]
    assert A.tenant in s._table and s._table[A.tenant].store is not None   # one holder, in the table, open
    assert s.count(A) == 1
    s.close()
