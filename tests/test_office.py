"""
AI Office: the data functions, the three read-only routes, the fixed static files, the key
behaviour (page public, data protected) and tripwires on the page's safety.
"""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

from core import autonomy, budget, office, tasks
from core.employees._db import get_connection

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATIC = ROOT / "core" / "office_static"


def sql(stmt, params=()):
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(stmt, params or None)  # an empty tuple would make psycopg2 read the % in LIKE patterns as placeholders
        conn.commit()
        cur.close()
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def cleanup():
    yield
    sql("DELETE FROM llm_usage WHERE provider = 'testoffice'")
    sql("DELETE FROM autonomy_log WHERE content LIKE 'TEST-OFFICE%'")
    sql("DELETE FROM autonomy_pending WHERE content LIKE 'TEST-OFFICE%'")
    sql("DELETE FROM tasks WHERE title LIKE 'TEST-OFFICE%'")


# ---- data ----

def test_overview_counts_and_shape():
    base = office.overview()
    sql("INSERT INTO autonomy_pending (action_id, action_type, channel, target, content) "
        "VALUES ('TESTOFF1', 'run_code', 'code', '', 'TEST-OFFICE pending')")
    tasks.create_task(title="TEST-OFFICE task")
    after = office.overview()
    assert after["pending_approvals"] == base["pending_approvals"] + 1
    assert after["tasks"].get("open", 0) == base["tasks"].get("open", 0) + 1
    assert after["autonomy"]["mode"] in ("off", "semi", "full")
    assert isinstance(after["autonomy"]["approval_target_set"], bool)
    assert set(after["usage"]) == {"day_tokens", "month_tokens"}
    assert set(after["triggers"]) == {"total", "active", "next_run_at"}
    assert isinstance(after["roles"]["total"], int)


def test_overview_never_reports_the_approval_target(monkeypatch):
    monkeypatch.setattr(office.autonomy, "get_autonomy_settings", lambda: {
        "mode": "semi", "channels": [], "actions": [],
        "approval_channel": "telegram", "approval_target": "SECRET-CHAT-ID"})
    text = json.dumps(office.overview())
    assert "SECRET-CHAT-ID" not in text and '"approval_target_set": true' in text


def test_autonomy_log_order_filter_truncation_and_clamp():
    autonomy.log_autonomous_action("run_code", "code", "", "TEST-OFFICE one", "Code run: exit 0", approved=True, role="r1")
    autonomy.log_autonomous_action("run_code", "code", "", "TEST-OFFICE two", "REJECTED by owner", approved=False, role="r1")
    autonomy.log_autonomous_action("run_code", "code", "", "TEST-OFFICE " + "x" * 5000, "ok", approved=True)
    rows = office.autonomy_log(limit=3)
    assert len(rows[0]["content"]) <= office.MAX_LOG_TEXT and rows[1]["content"] == "TEST-OFFICE two"
    assert isinstance(rows[0]["created_at"], str)
    rejected = office.autonomy_log(limit=50, approved=False)
    assert rejected and all(r["approved"] is False for r in rejected)
    assert any(r["content"] == "TEST-OFFICE two" for r in rejected)
    assert len(office.autonomy_log(limit=10 ** 6)) <= 200 and len(office.autonomy_log(limit=0)) <= 1


def test_usage_summary_totals_limits_and_sensitivity(monkeypatch):
    monkeypatch.setenv("BUDGET_ROLE_DAILY_TOKENS", "100")
    monkeypatch.setenv("BUDGET_DAILY_TOKENS", "1000000")
    monkeypatch.setattr(office.employee_roles, "list_roles", lambda active_only=False: [
        {"role": "legal_intake", "display_name": "Legal intake", "is_active": True},
        {"role": "sales", "display_name": "Sales", "is_active": False}])
    for role in ("zz_office_role", "zz_office_role", None):
        sql("INSERT INTO llm_usage (role, provider, model, prompt_tokens, completion_tokens, total_tokens, estimated) "
            "VALUES (%s, 'testoffice', 'm', 10, 5, 15, true)", (role,))
    s = office.usage_summary()
    by = {r["role"]: r for r in s["roles"]}
    z = by["zz_office_role"]
    assert (z["day_used"], z["month_used"], z["calls"]) == (30, 30, 2)
    assert z["day_limit"] == 100 and z["month_limit"] == 0 and z["sensitive"] is False
    assert by["legal_intake"]["sensitive"] is True and by["legal_intake"]["day_used"] == 0
    assert by["sales"]["is_active"] is False and by["sales"]["sensitive"] is False
    assert s["global"]["day_limit"] == 1000000 and s["global"]["day_used"] >= 45
    assert s["unattributed"]["day_used"] >= 15


def test_budget_limits_helper(monkeypatch):
    monkeypatch.setenv("BUDGET_DAILY_TOKENS", "500")
    monkeypatch.setenv("BUDGET_ROLE_DAILY_OVERRIDES", "ops=70")
    monkeypatch.setenv("BUDGET_ROLE_MONTHLY_TOKENS", "900")
    assert budget.limits(None) == {"day": 500, "month": 0}
    assert budget.limits("ops") == {"day": 70, "month": 900}
    assert budget.limits("other") == {"day": 0, "month": 900}


# ---- static files ----

def test_static_files_and_headers():
    for path in ("/office", "/office/app.js", "/office/app.css"):
        body, ctype, headers = office.static_response(path)
        assert body and ctype.startswith(("text/html", "text/javascript", "text/css"))
        assert "default-src 'none'" in headers["Content-Security-Policy"]
        assert "script-src 'self'" in headers["Content-Security-Policy"]
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        assert headers["X-Content-Type-Options"] == "nosniff" and headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("path", ["/office/", "/office/../.env", "/office/app.js/../../core/api.py",
                                  "/office/index.html", "/", "/office/x", ""])
def test_only_the_three_paths_are_served(path):
    assert office.static_response(path) is None


def test_page_safety_tripwires():
    forbidden = ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "ev" + "al(", "new " + "Function",
                 "javascript:", "srcdoc")
    js = (STATIC / "app.js").read_text()
    for token in forbidden:
        assert token not in js, token
    html = (STATIC / "index.html").read_text()
    assert not re.search(r"<script(?![^>]*\bsrc=)", html), "inline script"
    assert not re.search(r"\son[a-z]+\s*=", html), "inline event handler"
    assert "style=" not in html
    for name in ("index.html", "app.js", "app.css"):
        text = (STATIC / name).read_text()
        assert "http://" not in text and "https://" not in text, name      # nothing external
        assert "@import" not in text and "url(" not in text, name


def test_page_uses_the_expected_endpoints():
    js = (STATIC / "app.js").read_text()
    for path in ("/overview", "/autonomy/pending", "/agent-runs", "/autonomy/log"):
        assert path in js, path
    assert "sessionStorage" in js and "localStorage" not in js


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_app_js_is_valid_javascript():
    res = subprocess.run(["node", "--check", str(STATIC / "app.js")], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


# ---- key behaviour ----

def test_exempt_paths_are_exactly_the_page_files_and_the_sign_in_routes():
    # Changed on purpose with single sign-on: the person has no key yet when they reach these three routes.
    from core import api
    assert api.API_KEY_EXEMPT_PATHS == {"/health", "/office", "/office/app.js", "/office/app.css",
                                        "/auth/config", "/auth/login", "/auth/callback"}


def test_page_is_public_but_data_needs_the_key(monkeypatch):
    from fastapi.testclient import TestClient
    from core import api
    monkeypatch.setattr(api, "RAGLEAP_API_KEY", "k-test")
    c = TestClient(api.app)
    for path in ("/office", "/office/app.js", "/office/app.css", "/health"):
        assert c.get(path).status_code == 200, path
    page = c.get("/office")
    assert "script-src 'self'" in page.headers["Content-Security-Policy"] and "<script" in page.text
    for path in ("/overview", "/autonomy/log", "/usage/summary", "/autonomy/pending", "/agent-runs"):
        assert c.get(path).status_code == 401, path
        assert c.get(path, headers={"x-api-key": "wrong"}).status_code == 401, path
        assert c.get(path, headers={"x-api-key": "k-test"}).status_code == 200, path
