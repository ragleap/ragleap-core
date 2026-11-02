import io
import json
import os
import stat
from datetime import datetime, timedelta, timezone

import pytest

from ragleap_memory import (ImportResult, InvalidEntryError, QuotaExceededError, ShardedStore, SQLiteStore)
from ragleap_security import AuditLog, LimitError, Scope, verify

A = Scope("acme")
A_ALICE = Scope("acme", subject="alice")
B = Scope("globex")
HEADER = {"format": "ragleap-memory-export", "version": 1}
SECRET = "very private sentence"


def write_export(path, entries, count=None, header=None):
    h = header if header is not None else {**HEADER, "count": len(entries) if count is None else count}
    path.write_text("\n".join([json.dumps(h)] + [json.dumps(e) for e in entries]) + "\n")
    return path


def entry(**kw):
    e = {"scope": "tenant=acme", "kind": "fact", "text": "hello world", "trust": "owner"}
    e.update(kw)
    return e


def texts(store, scope):
    return sorted(e["text"] for e in store.export(scope))


# ---------------------------------------------------------------- export file

def test_export_file_has_a_header_and_one_line_per_entry(store, tmp_path):
    store.add(A, "one")
    old = store.add(A, "two")
    store.supersede(A, old.id, "two again")
    store.add(B, "other tenant")
    p = tmp_path / "out.jsonl"
    assert store.export_file(A, p) == 3                     # superseded entries are included
    lines = p.read_text().splitlines()
    assert json.loads(lines[0]) == {"count": 3, "format": "ragleap-memory-export", "version": 1}
    assert len(lines) == 4 and "other tenant" not in p.read_text()


def test_export_file_is_private_and_never_overwrites(store, tmp_path):
    store.add(A, "one")
    p = tmp_path / "out.jsonl"
    store.export_file(A, p)
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    before = p.read_bytes()
    with pytest.raises(FileExistsError):
        store.export_file(A, p)
    assert p.read_bytes() == before


def test_a_failed_export_leaves_no_partial_file(store, tmp_path, monkeypatch):
    monkeypatch.setattr(store, "export", lambda scope: [{"x": {1, 2}}])   # cannot be written as JSON
    p = tmp_path / "out.jsonl"
    with pytest.raises(TypeError):
        store.export_file(A, p)
    assert not p.exists()


# ---------------------------------------------------------------- round trip

def test_round_trip_keeps_content_history_and_links(tmp_path):
    src = SQLiteStore()
    future = datetime.now(timezone.utc) + timedelta(days=30)
    first = src.add(A, "prefers email", trust="owner", importance=0.9, metadata={"k": [1, 2]}, expires_at=future)
    base = src.add(A, "works in Chennai", source="chat")
    newer = src.supersede(A, first.id, "prefers phone calls")
    src.add(A, "summary of both", kind="summary", derived_from=[base.id, newer.id])
    p = tmp_path / "m.jsonl"
    src.export_file(A, p)

    dst = SQLiteStore()
    res = dst.import_file(A, p, max_trust="owner")
    assert res == ImportResult(imported=4, skipped_duplicates=0, dropped_links=0)
    by_text = {e["text"]: e for e in dst.export(A)}
    old_by_text = {e["text"]: e for e in src.export(A)}
    for t, e in by_text.items():
        o = old_by_text[t]
        for f in ("kind", "trust", "importance", "created_at", "expires_at", "metadata", "source", "tokens"):
            assert e[f] == o[f], (t, f)
        assert e["id"] != o["id"]                           # ids are always new
    assert by_text["prefers email"]["superseded_by"] == by_text["prefers phone calls"]["id"]
    assert sorted(by_text["summary of both"]["derived_from"]) == sorted(
        [by_text["works in Chennai"]["id"], by_text["prefers phone calls"]["id"]])
    assert [h.entry.text for h in dst.search(A, "phone")] == ["prefers phone calls"]   # and it is searchable


def test_unicode_and_line_separators_survive(store, tmp_path):
    text = "caf\u00e9 \U0001F600 line\u2028separator \"quoted\" \\ back"
    store.add(A, text)
    p = tmp_path / "u.jsonl"
    store.export_file(A, p)
    dst = SQLiteStore()
    dst.import_file(A, p)
    assert texts(dst, A) == [text]


def test_expired_entries_come_back_hidden(tmp_path):
    p = write_export(tmp_path / "e.jsonl", [entry(text="old news", expires_at="2020-01-01T00:00:00+00:00")])
    s = SQLiteStore()
    s.import_file(A, p)
    assert s.search(A, "news") == []
    assert len(s.search(A, "news", include_expired=True)) == 1


def test_import_from_a_stream(store, tmp_path):
    p = write_export(tmp_path / "s.jsonl", [entry()])
    res = store.import_stream(A, io.BytesIO(p.read_bytes()))
    assert res.imported == 1


# ---------------------------------------------------------------- trust

def test_trust_is_capped_by_default(store, tmp_path):
    p = write_export(tmp_path / "t.jsonl", [entry(text="a1", trust="owner"), entry(text="a2", trust="model"),
                                            entry(text="a3", trust="untrusted")])
    store.import_file(A, p)
    assert {e["trust"] for e in store.export(A)} == {"untrusted"}   # a file cannot promote itself


@pytest.mark.parametrize("cap,expected", [("untrusted", {"a1": "untrusted", "a2": "untrusted", "a3": "untrusted"}),
                                          ("model", {"a1": "model", "a2": "model", "a3": "untrusted"}),
                                          ("owner", {"a1": "owner", "a2": "model", "a3": "untrusted"})])
def test_max_trust_caps_but_never_raises(store, tmp_path, cap, expected):
    p = write_export(tmp_path / "t.jsonl", [entry(text="a1", trust="owner"), entry(text="a2", trust="model"),
                                            entry(text="a3", trust="untrusted")])
    store.import_file(A, p, max_trust=cap)
    assert {e["text"]: e["trust"] for e in store.export(A)} == expected


# ---------------------------------------------------------------- scope

def test_an_entry_outside_the_import_scope_stops_the_import(store, tmp_path):
    p = write_export(tmp_path / "x.jsonl", [entry(text="mine"), entry(scope="tenant=globex", text="theirs")])
    with pytest.raises(InvalidEntryError, match="line 3"):
        store.import_file(A, p)
    assert store.count(A) == 0 and store.count(B) == 0       # nothing at all was imported


def test_rescope_moves_a_file_into_the_target_tenant(store, tmp_path):
    p = write_export(tmp_path / "x.jsonl", [entry(scope="tenant=globex", text="moved"),
                                            entry(scope="tenant=globex/subject=alice", text="alice note")])
    store.import_file(A, p, rescope=True)
    assert store.count(B) == 0
    assert texts(store, A) == ["alice note", "moved"]
    assert [e.text for e in [store.search(A_ALICE, "alice")[0].entry]] == ["alice note"]


def test_a_narrow_import_scope_refuses_wider_entries(store, tmp_path):
    p = write_export(tmp_path / "x.jsonl", [entry(scope="tenant=acme/subject=alice", text="fine"),
                                            entry(scope="tenant=acme", text="too wide")])
    with pytest.raises(InvalidEntryError, match="outside"):
        store.import_file(A_ALICE, p)
    assert store.count(A) == 0
    store.import_file(A_ALICE, write_export(tmp_path / "y.jsonl", [entry(scope="tenant=acme/subject=alice")]))
    assert store.count(A) == 1


def test_file_ids_are_never_reused(store, tmp_path):
    p = write_export(tmp_path / "x.jsonl", [entry(id="a" * 32)])
    store.import_file(A, p)
    assert store.export(A)[0]["id"] != "a" * 32


# ---------------------------------------------------------------- duplicates

def test_importing_the_same_file_twice_adds_nothing(store, tmp_path):
    p = write_export(tmp_path / "x.jsonl", [entry(text="one"), entry(text="two"), entry(text="one")])
    assert store.import_file(A, p) == ImportResult(2, 1, 0)   # a duplicate inside the file counts too
    assert store.import_file(A, p) == ImportResult(0, 3, 0)
    assert store.count(A) == 2
    assert store.import_file(A, p, skip_duplicates=False).imported == 3
    assert store.count(A) == 5


def test_the_same_text_in_another_scope_is_not_a_duplicate(store, tmp_path):
    store.add(A, "shared text")
    p = write_export(tmp_path / "x.jsonl", [entry(scope="tenant=acme/subject=alice", text="shared text")])
    assert store.import_file(A, p).imported == 1


# ---------------------------------------------------------------- links

def test_links_outside_the_file_are_dropped_and_counted(store, tmp_path):
    p = write_export(tmp_path / "x.jsonl", [entry(derived_from=["f" * 32], superseded_by="e" * 32)])
    res = store.import_file(A, p)
    assert res == ImportResult(1, 0, 2)
    e = store.export(A)[0]
    assert e["derived_from"] == [] and e["superseded_by"] is None


def test_links_that_cross_scopes_are_dropped(store, tmp_path):
    p = write_export(tmp_path / "x.jsonl", [
        entry(id="1" * 32, text="tenant level"),
        entry(id="2" * 32, scope="tenant=acme/subject=alice", text="alice level", derived_from=["1" * 32]),
        entry(id="3" * 32, text="old", superseded_by="2" * 32),   # superseder lives in another scope
    ])
    res = store.import_file(A, p)
    assert res.dropped_links == 2
    assert all(e["derived_from"] == [] and e["superseded_by"] is None for e in store.export(A))


def test_a_wide_scope_entry_may_derive_from_a_narrow_one_only_if_it_covers_it(store, tmp_path):
    p = write_export(tmp_path / "x.jsonl", [
        entry(id="1" * 32, scope="tenant=acme/subject=alice", text="alice fact"),
        entry(id="2" * 32, text="summary", kind="summary", derived_from=["1" * 32]),
    ])
    assert store.import_file(A, p).dropped_links == 0         # tenant scope covers alice's entry: kept
    s = {e["text"]: e for e in store.export(A)}
    assert s["summary"]["derived_from"] == [s["alice fact"]["id"]]


# ---------------------------------------------------------------- all or nothing

def test_one_bad_entry_imports_nothing_and_the_error_has_no_text(tmp_path):
    log = AuditLog(str(tmp_path / "audit.jsonl"))
    s = SQLiteStore(audit=log)
    p = write_export(tmp_path / "x.jsonl", [entry(text="fine one"), entry(text="fine two"), entry(text="fine three"),
                                            entry(text=SECRET, kind="nonsense"), entry(text="fine five")])
    with pytest.raises(InvalidEntryError) as ei:
        s.import_file(A, p)
    assert "line 5" in str(ei.value) and SECRET not in str(ei.value)
    assert s.count(A) == 0
    assert verify(str(tmp_path / "audit.jsonl")).records == 0


def test_a_quota_stops_the_whole_import(tmp_path):
    s = SQLiteStore(quota_entries=2)
    p = write_export(tmp_path / "x.jsonl", [entry(text="a1"), entry(text="a2"), entry(text="a3")])
    with pytest.raises(QuotaExceededError):
        s.import_file(A, p)
    assert s.count(A) == 0
    s.import_file(A, write_export(tmp_path / "y.jsonl", [entry(text="a1"), entry(text="a2")]))   # exactly at it
    assert s.count(A) == 2


def test_an_import_is_audited_with_counts_only(tmp_path):
    path = str(tmp_path / "audit.jsonl")
    s = SQLiteStore(audit=AuditLog(path))
    p = write_export(tmp_path / "x.jsonl", [entry(text=SECRET)])
    s.import_file(A, p)
    assert verify(path).ok
    last = json.loads(open(path).read().splitlines()[-1])
    assert last["event"] == "memory.import" and last["data"] == {"imported": 1, "skipped": 0}
    assert SECRET not in open(path).read()


# ---------------------------------------------------------------- hostile files

@pytest.mark.parametrize("header", [
    {"format": "something-else", "version": 1, "count": 0}, {"format": "ragleap-memory-export", "version": 2, "count": 0},
    {"format": "ragleap-memory-export", "version": True, "count": 0}, {"format": "ragleap-memory-export", "version": 1},
    {"format": "ragleap-memory-export", "version": 1, "count": -1},
    {"format": "ragleap-memory-export", "version": 1, "count": True},
    {"format": "ragleap-memory-export", "version": 1, "count": "0"}, [], "text"])
def test_bad_headers_are_refused(store, tmp_path, header):
    p = write_export(tmp_path / "x.jsonl", [], header=header)
    with pytest.raises(InvalidEntryError):
        store.import_file(A, p)


def test_missing_header_or_empty_file_is_refused(store, tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text(json.dumps(entry()) + "\n")
    with pytest.raises(InvalidEntryError):
        store.import_file(A, p)
    p.write_text("")
    with pytest.raises(InvalidEntryError):
        store.import_file(A, p)
    assert store.count(A) == 0


def test_a_truncated_or_padded_file_is_refused(store, tmp_path):
    with pytest.raises(InvalidEntryError, match="count"):
        store.import_file(A, write_export(tmp_path / "a.jsonl", [entry(text="a1"), entry(text="a2")], count=3))
    with pytest.raises(InvalidEntryError, match="count"):
        store.import_file(A, write_export(tmp_path / "b.jsonl", [entry(text="a1"), entry(text="a2")], count=1))
    assert store.count(A) == 0


@pytest.mark.parametrize("line", ["not json at all", "[1, 2]", "\"text\"", "{\"scope\": \"tenant=acme\"}",
                                  json.dumps(entry(surprise=1)), json.dumps(entry(scope=5)),
                                  json.dumps(entry(scope="tenant=acme/../x")), json.dumps(entry(trust="root")),
                                  json.dumps(entry(kind="nonsense")), json.dumps(entry(text="   ")),
                                  json.dumps(entry(text=5)), json.dumps(entry(importance=2)),
                                  json.dumps(entry(importance=True)), json.dumps(entry(source="bad source!")),
                                  json.dumps(entry(created_at="yesterday")), json.dumps(entry(created_at="2026-01-01T00:00:00")),
                                  json.dumps(entry(expires_at="2026-01-01T00:00:00")), json.dumps(entry(expires_at=5)),
                                  json.dumps(entry(id="not an id")), json.dumps(entry(id=5)),
                                  json.dumps(entry(derived_from="x")), json.dumps(entry(derived_from=["bad"])),
                                  json.dumps(entry(derived_from=["a" * 32] * 101)), json.dumps(entry(superseded_by="bad")),
                                  json.dumps(entry(metadata=[1])), json.dumps(entry(metadata={"k": "x" * 5000}))])
def test_bad_entries_are_refused_with_their_line_number(store, tmp_path, line):
    p = tmp_path / "x.jsonl"
    p.write_text(json.dumps({**HEADER, "count": 1}) + "\n" + line + "\n")
    with pytest.raises((InvalidEntryError, LimitError), match="line 2"):
        store.import_file(A, p)
    assert store.count(A) == 0


def test_deep_or_huge_values_hit_the_limits(store, tmp_path):
    deep = {"k": 1}
    for _ in range(12):
        deep = {"k": deep}
    for e in (entry(metadata=deep), entry(text="x" * 9000)):
        p = write_export(tmp_path / "x.jsonl", [e])
        with pytest.raises(LimitError, match="line 2"):
            store.import_file(A, p)
    assert store.count(A) == 0


def test_size_limits_apply_to_lines_entries_and_the_file(store, tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text(json.dumps({**HEADER, "count": 1}) + "\n" + json.dumps(entry(text="x" * 70000)) + "\n")
    with pytest.raises(LimitError, match="line"):
        store.import_file(A, p)
    p = write_export(tmp_path / "y.jsonl", [entry(text=f"a{i}") for i in range(5)])
    with pytest.raises(LimitError, match="entries"):
        store.import_file(A, p, max_entries=4)
    store.import_file(A, p, max_entries=5)
    with pytest.raises(LimitError, match="larger"):
        store.import_file(A, p, max_bytes=100, skip_duplicates=False)
    assert store.count(A) == 5


@pytest.mark.parametrize("name,bad", [("max_trust", "root"), ("max_entries", 0), ("max_entries", True),
                                      ("max_bytes", -1), ("max_bytes", 1.5)])
def test_bad_import_options_are_refused(store, tmp_path, name, bad):
    p = write_export(tmp_path / "x.jsonl", [entry()])
    with pytest.raises(InvalidEntryError):
        store.import_file(A, p, **{name: bad})
    assert store.count(A) == 0


def test_blank_lines_are_ignored(store, tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text(json.dumps({**HEADER, "count": 1}) + "\n\n" + json.dumps(entry()) + "\n\n")
    assert store.import_file(A, p).imported == 1


# ---------------------------------------------------------------- sharded

def test_sharded_export_and_import(tmp_path):
    s = ShardedStore(tmp_path / "mem")
    s.add(A, "acme fact")
    p = tmp_path / "a.jsonl"
    assert s.export_file(A, p) == 1
    with pytest.raises(InvalidEntryError):                    # another tenant cannot take acme's file as its own
        s.import_file(B, p)
    assert s.count(B) == 0
    assert s.import_file(B, p, rescope=True).imported == 1
    assert s.import_stream(B, io.BytesIO(p.read_bytes()), rescope=True).skipped_duplicates == 1
    assert texts(s, B) == ["acme fact"] and s.count(A) == 1
    s.close()


def test_the_entry_cap_holds_even_if_the_header_understates_the_count(store, tmp_path):
    p = write_export(tmp_path / "x.jsonl", [entry(text=f"a{i}") for i in range(5)], count=1)
    with pytest.raises(LimitError, match="entries"):
        store.import_file(A, p, max_entries=2)
    assert store.count(A) == 0
