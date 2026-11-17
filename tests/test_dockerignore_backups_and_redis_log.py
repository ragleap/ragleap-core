"""Database dumps stay out of the image build (INFRA-F-001) and Redis credentials stay out of logs (F-CORE-001)."""
import logging
import os
import sys
import types
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import queue

ROOT = Path(__file__).resolve().parent.parent


def test_dockerignore_keeps_update_backups_out_of_the_image():
    lines = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "backups/" in lines
    assert "**/*_pre-update.sql" in lines


def test_dockerignore_does_not_hide_the_schema():
    lines = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert not any(l.strip() in ("*.sql", "db/", "db/schema.sql") for l in lines)


def test_redact_url_hides_user_and_password():
    assert queue._redact_url("redis://:s3cret@cache:6379/0") == "redis://***@cache:6379/0"
    assert queue._redact_url("redis://user:s3cret@cache:6379/0") == "redis://***@cache:6379/0"
    assert queue._redact_url("redis://redis:6379/0") == "redis://redis:6379/0"
    assert queue._redact_url("") == ""


def test_the_connected_log_line_has_no_password(monkeypatch, caplog):
    fake_redis = types.SimpleNamespace(from_url=lambda url: types.SimpleNamespace(ping=lambda: True))
    fake_rq = types.SimpleNamespace(Queue=lambda name, connection=None: object())
    monkeypatch.setitem(sys.modules, "redis", fake_redis)
    monkeypatch.setitem(sys.modules, "rq", fake_rq)
    monkeypatch.setattr(queue, "REDIS_URL", "redis://:s3cret@cache:6379/0")
    monkeypatch.setattr(queue, "_queue", None)
    monkeypatch.setattr(queue, "_connection", None)
    with caplog.at_level(logging.INFO, logger=queue.logger.name):
        assert queue._get_queue() is not None
    assert "s3cret" not in caplog.text
    assert "***@cache" in caplog.text
