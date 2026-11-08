import json
import os
import sqlite3

import pytest

from ragleap_memory import InvalidEntryError, SQLiteStore, sqlite_store as ss
from ragleap_security import Scope

from fake_embed import DIM, MODEL, FakeEmbedder, embed_text

A = Scope("acme")
B = Scope("globex")


def make(path=":memory:", **kw):
    return SQLiteStore(path, embedder=FakeEmbedder(), embedding_model=MODEL, embedding_dim=DIM, **kw)


def raw(path):
    return b"".join(open(str(path) + ext, "rb").read() for ext in ("", "-wal") if os.path.exists(str(path) + ext))


def blob_of(text):
    return ss._to_blob(ss._unit_vector(embed_text(text), DIM))


def test_erase_removes_the_vector_bytes_from_the_file(tmp_path):
    path = tmp_path / "mem.db"
    s = make(path)
    s.add(A, "an automobile for acme")
    s.add(B, "a truck for globex")
    s.embed_pending(A)
    s.embed_pending(B)
    s.compact()
    mine, theirs = blob_of("an automobile for acme"), blob_of("a truck for globex")
    assert mine in raw(path) and theirs in raw(path)                  # positive control: the vectors are in the file
    s.erase(A)
    assert mine not in raw(path)
    assert theirs in raw(path)                                        # the other tenant is untouched


def test_forget_then_compact_removes_the_vector_bytes(tmp_path):
    path = tmp_path / "mem.db"
    s = make(path)
    e = s.add(A, "an automobile for acme")
    s.embed_pending(A)
    s.compact()
    assert blob_of("an automobile for acme") in raw(path)
    s.forget(A, [e.id])
    s.compact()
    assert blob_of("an automobile for acme") not in raw(path)


def test_export_has_no_vectors(tmp_path):
    s = make()
    s.add(A, "an automobile")
    s.embed_pending(A)
    for d in s.export(A):
        assert not any("vec" in k or "embed" in k for k in d)
    n = s.export_file(A, tmp_path / "out.jsonl")
    assert n == 1
    text = (tmp_path / "out.jsonl").read_text()
    assert "vec" not in text and "embed" not in text


def test_an_import_refuses_a_vector_smuggled_into_an_entry(tmp_path):
    s = make()
    s.add(A, "an automobile")
    s.export_file(A, tmp_path / "out.jsonl")
    lines = (tmp_path / "out.jsonl").read_text().splitlines()
    entry = json.loads(lines[1])
    entry["embedding"] = [0.1] * DIM
    (tmp_path / "bad.jsonl").write_text("\n".join([lines[0], json.dumps(entry)]) + "\n")
    t = make()
    with pytest.raises(InvalidEntryError):
        t.import_file(A, tmp_path / "bad.jsonl")
    assert t.count(A) == 0


def test_imported_entries_have_no_vector_until_embedded(tmp_path):
    s = make()
    s.add(A, "an automobile")
    s.embed_pending(A)
    s.export_file(A, tmp_path / "out.jsonl")
    t = make()
    t.import_file(A, tmp_path / "out.jsonl")
    assert t._db.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 0
    assert t.search(A, "car", mode="vector") == []
    assert t.embed_pending(A).embedded == 1
    assert len(t.search(A, "car", mode="vector")) == 1


# -- migration from schema 2 (what 0.2.0 wrote) ------------------------------------------------------

def build_schema_2(path):
    """A file laid out exactly as 0.2.0 leaves it: schema 1 plus the tokens column and the 0.2.0 tables."""
    db = sqlite3.connect(path)
    db.executescript(ss._SCHEMA_V1)
    db.execute("ALTER TABLE memories ADD COLUMN tokens INTEGER NOT NULL DEFAULT 0")
    for ddl in ss._V2_DDL:
        db.execute(ddl)
    db.execute("INSERT INTO memories(id, tenant, kind, text, source, trust, importance, created_at, derived_from,"
               " metadata, tokens) VALUES (?, 'acme', 'fact', 'an old automobile entry', 'api', 'untrusted', 0.5,"
               " '2026-11-01T00:00:00.000000+00:00', '[]', '{}', 8)", ["a" * 32])
    db.execute("INSERT INTO usage(tenant, period, served_tokens, searches) VALUES ('acme', '2026-11-01', 42, 7)")
    db.execute("PRAGMA user_version = 2")
    db.commit()
    db.close()


def test_a_schema_2_file_is_migrated_in_place(tmp_path):
    path = tmp_path / "old.db"
    build_schema_2(path)
    s = make(path)
    assert s._db.execute("PRAGMA user_version").fetchone()[0] == 3
    names = {r[0] for r in s._db.execute("SELECT name FROM sqlite_master")}
    assert {"embeddings", "embeddings_ad"} <= names
    cols = {r["name"] for r in s._db.execute("PRAGMA table_info(usage)")}
    assert {"embedded_tokens", "embed_calls"} <= cols
    u = s.usage(A, "2026-11-01")
    assert (u["served_tokens"], u["searches"], u["embedded_tokens"], u["embed_calls"]) == (42, 7, 0, 0)
    assert [h.entry.text for h in s.search(A, "automobile")] == ["an old automobile entry"]
    assert s.embed_pending(A).embedded == 1
    assert len(s.search(A, "car", mode="vector")) == 1


def test_reopening_a_schema_3_file_changes_nothing(tmp_path):
    path = tmp_path / "mem.db"
    s = make(path)
    e = s.add(A, "an automobile")
    s.embed_pending(A)
    before = s.usage(A)
    s.close()
    t = make(path)
    assert t._db.execute("PRAGMA user_version").fetchone()[0] == 3
    assert t.get(A, e.id) == e and t.usage(A) == before
    assert t._db.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 1


def test_a_file_without_an_embedder_still_opens_and_keeps_its_vectors(tmp_path):
    path = tmp_path / "mem.db"
    s = make(path)
    s.add(A, "an automobile")
    s.embed_pending(A)
    s.close()
    t = SQLiteStore(path)
    assert t.count(A) == 1
    assert t._db.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 1


def test_the_vector_table_follows_its_entry_by_trigger():
    s = make()
    e = s.add(A, "an automobile")
    s.embed_pending(A)
    s._db.execute("DELETE FROM memories WHERE id = ?", [e.id])
    assert s._db.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 0
