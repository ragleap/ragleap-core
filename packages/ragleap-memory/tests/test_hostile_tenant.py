"""Attacks a hostile tenant would try against a shared store. Every test also holds a positive control
(the victim's data is really there), so an empty result cannot pass by accident."""
import json
import os
import random
import stat
import threading

import pytest

from ragleap_memory import InvalidEntryError, NotFoundError, ShardedStore, SQLiteStore
from ragleap_security import Scope

A, B = Scope("acme"), Scope("globex")
WORD = "quarterly forecast"


@pytest.fixture(params=["single", "sharded"])
def shared(request, tmp_path):
    s = SQLiteStore() if request.param == "single" else ShardedStore(tmp_path / "shards")
    yield s
    s.close()


@pytest.fixture
def two(shared):
    a = shared.add(A, f"acme {WORD}")
    b = shared.add(B, f"globex {WORD}")
    return shared, a, b


def test_foreign_and_unknown_ids_give_the_same_message(two):
    store, a, b = two
    unknown = "0" * 32
    for call in (lambda i: store.get(A, i),
                 lambda i: store.supersede(A, i, "overwritten"),
                 lambda i: store.add(A, "x", derived_from=[i])):
        with pytest.raises(NotFoundError) as foreign:
            call(b.id)
        with pytest.raises(NotFoundError) as missing:
            call(unknown)
        assert str(foreign.value) == str(missing.value)
    assert store.get(B, b.id).superseded_by is None and store.count(B) == 1 and store.count(A) == 1


QUERIES = ["scope:globex", "tenant:globex", "tenant: globex forecast", "merg*", "forecast*", "forecast NEAR globex",
           'forecast" OR "globex', "'; DROP TABLE memories; --", "forecast; DELETE FROM memories", "\\", "\"", "(", ")",
           "-globex", "^forecast", "forecast\x00globex", "forecast\u2028globex", "%", "_"]


@pytest.mark.parametrize("query", QUERIES)
def test_hostile_queries_never_return_another_tenant_and_never_damage_tables(two, query):
    store, a, b = two
    try:
        hits = store.search(A, query, k=10)
    except InvalidEntryError:
        hits = []                                    # refusing a bad query is fine; leaking is not
    assert b.id not in {h.entry.id for h in hits}
    assert [h.entry.id for h in store.search(A, WORD)] == [a.id]     # positive control, tables intact
    assert [h.entry.id for h in store.search(B, WORD)] == [b.id]
    assert store.count(A) == 1 and store.count(B) == 1


@pytest.mark.parametrize("bad", ["%", "_" * 32, "0" * 31 + "%", "' OR 1=1 --", "a" * 33, "", "0" * 32 + "\n"])
def test_wildcard_and_injection_ids_delete_nothing(two, bad):
    store, a, b = two
    try:
        assert store.forget(A, [bad]) == 0
    except InvalidEntryError:
        pass
    with pytest.raises((NotFoundError, InvalidEntryError)):
        store.get(A, bad)
    assert store.count(A) == 1 and store.count(B) == 1


def test_forget_with_foreign_ids_deletes_nothing_and_says_zero(two):
    store, a, b = two
    assert store.forget(A, [b.id]) == 0
    assert store.get(B, b.id).text.startswith("globex")


def test_a_crafted_file_cannot_write_into_or_link_to_another_tenant(shared, tmp_path):
    a = shared.add(A, f"acme {WORD}")
    header = {"format": "ragleap-memory-export", "version": 1, "count": 2}
    mine = {"id": "1" * 32, "scope": "tenant=globex", "kind": "fact", "text": "planted", "trust": "owner"}
    p = tmp_path / "evil.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in (header, mine, mine)) + "\n")
    with pytest.raises(InvalidEntryError):
        shared.import_file(A, p)                       # entries claim globex, the target is acme
    assert shared.count(A) == 1 and shared.count(B) == 0
    link = {"scope": "tenant=globex", "kind": "fact", "text": "linked", "trust": "owner",
            "derived_from": [a.id], "superseded_by": a.id}
    p.write_text("\n".join(json.dumps(x) for x in ({**header, "count": 1}, link)) + "\n")
    res = shared.import_file(B, p)
    assert res.dropped_links == 2
    got = shared.export(B)[0]
    assert got["derived_from"] == [] and got["superseded_by"] is None and got["trust"] == "untrusted"
    assert shared.get(A, a.id).superseded_by is None


def test_tenants_whose_names_differ_only_in_case_do_not_share_files_or_data(tmp_path):
    s = ShardedStore(tmp_path / "shards")
    up, low = Scope("Acme"), Scope("acme")
    e = s.add(up, f"upper {WORD}")
    assert s.search(low, WORD) == [] and s.count(low) == 0
    with pytest.raises(NotFoundError):
        s.get(low, e.id)
    s.add(low, f"lower {WORD}")
    assert [h.entry.text for h in s.search(up, WORD)] == [f"upper {WORD}"]
    s.close()
    assert len({n for n in os.listdir(tmp_path / "shards") if n.endswith(".db")}) == 2


def test_database_wal_and_shm_files_stay_private_under_a_loose_umask(tmp_path):
    old = os.umask(0o022)
    try:
        path = tmp_path / "m.db"
        s = SQLiteStore(path)
        s.add(A, WORD)
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(str(path) + suffix):
                assert stat.S_IMODE(os.stat(str(path) + suffix).st_mode) == 0o600, suffix
        s.close()
        sh = ShardedStore(tmp_path / "sh")
        sh.add(A, WORD)
        assert stat.S_IMODE(os.stat(tmp_path / "sh").st_mode) == 0o700
        for n in os.listdir(tmp_path / "sh"):
            assert stat.S_IMODE(os.stat(tmp_path / "sh" / n).st_mode) == 0o600, n
        sh.close()
    finally:
        os.umask(old)


def test_a_tenant_wide_scope_reads_its_narrower_scopes_by_design_but_never_the_reverse(shared):
    alice = Scope("acme", "alice")
    ea, et = shared.add(alice, f"alice {WORD}"), shared.add(A, f"tenant {WORD}")
    assert {h.entry.id for h in shared.search(A, WORD, k=10)} == {ea.id, et.id}
    assert [h.entry.id for h in shared.search(alice, WORD)] == [ea.id]


def test_mixed_concurrent_operations_never_cross_tenants(shared, tmp_path):
    tenants = [Scope(f"t{i}") for i in range(4)]
    failures, lock = [], threading.Lock()

    def work(seed):
        rng = random.Random(seed)
        mine = {}
        for _ in range(60):
            sc = rng.choice(tenants)
            op = rng.choice(["add", "search", "get_foreign", "forget_foreign", "export"])
            try:
                if op == "add":
                    e = shared.add(sc, f"marker-{sc.tenant} {WORD} {rng.random()}")
                    mine.setdefault(sc.tenant, []).append(e.id)
                elif op == "search":
                    for h in shared.search(sc, WORD, k=20):
                        if f"marker-{sc.tenant} " not in h.entry.text:
                            failures.append(("search", sc.tenant, h.entry.text))
                elif op == "export":
                    for d in shared.export(sc):
                        if f"marker-{sc.tenant} " not in d["text"]:
                            failures.append(("export", sc.tenant))
                else:
                    other = [t for t in mine if t != sc.tenant]
                    if other:
                        victim = rng.choice(mine[rng.choice(other)])
                        if op == "get_foreign":
                            try:
                                shared.get(sc, victim)
                                failures.append(("get", sc.tenant))
                            except NotFoundError:
                                pass
                        elif shared.forget(sc, [victim]) != 0:
                            failures.append(("forget", sc.tenant))
            except Exception as e:                       # noqa: BLE001 - any other error is a finding
                failures.append(("error", type(e).__name__, str(e)))

    ts = [threading.Thread(target=work, args=(n,)) for n in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert failures == []
