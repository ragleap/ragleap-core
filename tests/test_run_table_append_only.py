"""The start records of sub-agents live in an append-only table (Phase 0, P0-J): no update or delete path from runtime code."""
import pathlib
import re
import uuid

import pytest

from core.employees._db import get_connection

ROOT = pathlib.Path(__file__).resolve().parent.parent
CORE_FILES = [p for p in (ROOT / "core").rglob("*.py")] + [p for p in (ROOT / "channels").rglob("*.py") if (ROOT / "channels").exists()]
TABLE = "agent_run_starts"


def test_only_the_agent_loop_module_touches_the_start_table():
    users = sorted(str(p.relative_to(ROOT)) for p in CORE_FILES if TABLE in p.read_text(encoding="utf-8", errors="ignore"))
    assert users == ["core/agent_loop.py"], users


def test_runtime_sql_on_the_start_table_is_only_insert_and_select():
    text = (ROOT / "core" / "agent_loop.py").read_text(encoding="utf-8")
    assert not re.search(r"(UPDATE|DELETE\s+FROM|TRUNCATE|DROP|ALTER|CREATE)\s+(TABLE\s+)?%s" % TABLE, text, re.I)
    mentions = re.findall(r"(INSERT\s+INTO\s+%s|FROM\s+%s)" % (TABLE, TABLE), text, re.I)
    assert len(mentions) == 3                                        # one insert, the lookup, and the key report
    for p in CORE_FILES:                                             # and nobody else builds SQL against it by name
        assert TABLE not in p.read_text(encoding="utf-8", errors="ignore") or p.name == "agent_loop.py"


def test_retention_never_lists_the_start_table():
    text = (ROOT / "core" / "retention.py").read_text(encoding="utf-8")
    assert TABLE not in text and "agent_runs" in text                 # runs may be purged when the operator asks; starts are not


def test_the_start_table_has_no_foreign_key_so_deleting_a_run_cannot_cascade_into_it():
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM information_schema.table_constraints WHERE table_name = %s AND constraint_type = 'FOREIGN KEY'", (TABLE,))
        assert cur.fetchone()[0] == 0
        cur.close()
    finally:
        conn.close()


def _insert(cur):
    sid, rid, pid = (str(uuid.uuid4()) for _ in range(3))
    cur.execute("INSERT INTO agent_run_starts (spawn_id, run_id, parent_id, role, depth, forced, key_id, sig) VALUES (%s, %s, %s, 'support', 1, false, 'k', 's')",
                (sid, rid, pid))
    return sid


@pytest.mark.parametrize("statement", ["UPDATE agent_run_starts SET forced = true", "UPDATE agent_run_starts SET sig = 'x'",
                                       "DELETE FROM agent_run_starts", "TRUNCATE agent_run_starts"])
def test_the_database_refuses_update_delete_and_truncate(statement):
    conn = get_connection()
    try:
        cur = conn.cursor()
        _insert(cur)                                                   # inside one transaction that is always rolled back
        with pytest.raises(Exception) as e:
            cur.execute(statement)
        assert "append-only" in str(e.value)
        conn.rollback()
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM agent_run_starts WHERE key_id = 'k' AND sig = 's'")
        assert cur.fetchone()[0] == 0                                   # nothing of the test stayed behind
        cur.close()
    finally:
        conn.rollback()
        conn.close()


def test_an_insert_is_allowed_and_a_second_record_for_the_same_run_is_not():
    conn = get_connection()
    try:
        cur = conn.cursor()
        sid, rid, pid = (str(uuid.uuid4()) for _ in range(3))
        sql = "INSERT INTO agent_run_starts (spawn_id, run_id, parent_id, role, depth, forced, key_id, sig) VALUES (%s, %s, %s, 'support', 1, false, 'k', 's')"
        cur.execute(sql, (sid, rid, pid))
        with pytest.raises(Exception):
            cur.execute(sql, (str(uuid.uuid4()), rid, pid))              # run_id is unique: one start record per child, ever
    finally:
        conn.rollback()
        conn.close()
