"""Retention of the audit trail: delete rows older than a number of days, only when the operator asks for it.

Settings (a number of days; empty or 0 means keep forever, which is the default; otherwise at least 7, so a typo
such as 1 instead of 180 cannot wipe the history):
  AUDIT_RETENTION_DAYS        audit_events: who changed what
  ACTION_LOG_RETENTION_DAYS   autonomy_log: what agents did and who approved or rejected it
  AGENT_RUN_RETENTION_DAYS    agent_runs that are finished (done, failed, rejected, budget); a run that is
                              running or waiting for approval is never deleted

The purge runs in small batches, on the scheduler leader only (see core/scheduler_leader.py), and every table that
loses rows writes one "retention_purge" audit log line with the number of rows deleted. Nothing here raises.
"""
import logging
import os
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

MIN_DAYS = 7
BATCH = 1000
MAX_BATCHES = 50          # per table and run; what is left is taken in the next run

# table, setting, time column, extra condition (a constant, never user input)
TABLES = (
    ("audit_events", "AUDIT_RETENTION_DAYS", "at", ""),
    ("autonomy_log", "ACTION_LOG_RETENTION_DAYS", "created_at", ""),
    ("agent_runs", "AGENT_RUN_RETENTION_DAYS", "created_at", " AND status IN ('done', 'failed', 'rejected', 'budget')"),
)


def days(setting: str) -> int:
    """The configured number of days, or 0 (keep forever) when it is empty, invalid or below MIN_DAYS."""
    raw = os.environ.get(setting, "").strip()
    if not raw:
        return 0
    try:
        n = int(raw)
    except ValueError:
        logger.warning("%s is not a whole number; keeping everything.", setting)
        return 0
    if n <= 0:
        return 0
    if n < MIN_DAYS:
        logger.warning("%s must be 0 or at least %d; keeping everything.", setting, MIN_DAYS)
        return 0
    return n


def enabled() -> bool:
    return any(days(setting) for _t, setting, _c, _x in TABLES)


def _connect():
    from core import document_store
    return document_store._connect()


def _emit(**fields) -> None:
    try:
        from core import audit
        audit.emit("retention_purge", **fields)
    except Exception:
        pass


def status(now: datetime = None) -> list:
    """Per table: the setting, the days, the cutoff and how many rows are older than it (None when kept forever)."""
    now = now or datetime.now(timezone.utc)
    out = []
    for table, setting, col, extra in TABLES:
        d = days(setting)
        item = {"table": table, "setting": setting, "days": d, "cutoff": None, "rows_due": None}
        if d:
            cutoff = now - timedelta(days=d)
            item["cutoff"] = cutoff.isoformat()
            try:
                conn = _connect()
                try:
                    cur = conn.cursor()
                    cur.execute("SELECT count(*) FROM %s WHERE %s < %%s%s" % (table, col, extra), (cutoff,))
                    item["rows_due"] = int(cur.fetchone()[0])
                finally:
                    conn.close()
            except Exception as e:
                item["error"] = e.__class__.__name__
        out.append(item)
    return out


def purge(trigger: str = "job", dry_run: bool = False, now: datetime = None) -> list:
    """Delete what is older than the configured limits (or only count it when dry_run). Never raises."""
    now = now or datetime.now(timezone.utc)
    if dry_run:
        return [dict(s, deleted=0) for s in status(now)]
    out = []
    for table, setting, col, extra in TABLES:
        d = days(setting)
        item = {"table": table, "setting": setting, "days": d, "cutoff": None, "deleted": 0}
        out.append(item)
        if not d:
            continue
        cutoff = now - timedelta(days=d)
        item["cutoff"] = cutoff.isoformat()
        try:
            conn = _connect()
            try:
                cur = conn.cursor()
                sql = ("DELETE FROM %s WHERE id IN (SELECT id FROM %s WHERE %s < %%s%s ORDER BY %s LIMIT %%s)"
                       % (table, table, col, extra, col))
                for _ in range(MAX_BATCHES):
                    cur.execute(sql, (cutoff, BATCH))
                    n = cur.rowcount or 0
                    conn.commit()
                    item["deleted"] += n
                    if n < BATCH:
                        break
            finally:
                conn.close()
        except Exception as e:
            item["error"] = e.__class__.__name__
            logger.error("Retention purge of %s failed: %s", table, item["error"])
        if item["deleted"] or item.get("error"):
            logger.info("Retention purge of %s: %d rows older than %d days deleted.", table, item["deleted"], d)
            _emit(table=table, retention_days=d, cutoff=item["cutoff"], deleted=item["deleted"],
                  trigger=trigger, error=item.get("error"))
    return out
