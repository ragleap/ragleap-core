"""
Data and static files for the AI Office, the owner's dashboard served at /office.

The page itself is three fixed files (core/office_static) served with a strict content
policy; the data routes (/overview, /autonomy/log, /usage/summary, ...) stay behind the API
key. Everything here is read-only and never reports secrets (the approval target is reported
only as set / not set).
"""
import logging
import pathlib
from typing import Dict, List, Optional, Tuple

from core import autonomy, budget
from core.employees import roles as employee_roles
from core.employees._db import get_connection
from core.employees.sensitivity import is_sensitive_role

logger = logging.getLogger(__name__)

STATIC_DIR = pathlib.Path(__file__).resolve().parent / "office_static"
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
       "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
_FILES = {
    "/office": ("index.html", "text/html"),
    "/office/app.js": ("app.js", "text/javascript"),
    "/office/app.css": ("app.css", "text/css"),
}
MAX_LOG_TEXT = 2000


def static_response(path: str) -> Optional[Tuple[bytes, str, Dict[str, str]]]:
    """(body, content_type, headers) for one of the three fixed paths, else None."""
    entry = _FILES.get(path)
    if entry is None:
        return None
    name, content_type = entry
    try:
        body = (STATIC_DIR / name).read_bytes()
    except OSError:
        logger.error("AI Office file missing: %s", name)
        return None
    return body, content_type, dict(SECURITY_HEADERS)


def _iso(value) -> Optional[str]:
    return value.isoformat() if value is not None else None


def overview() -> Dict:
    conn = get_connection()
    try:
        cur = conn.cursor()

        def one(sql):
            cur.execute(sql)
            return cur.fetchone()[0]

        def groups(sql):
            cur.execute(sql)
            return {k: int(v) for k, v in cur.fetchall()}

        out = {
            "pending_approvals": int(one("SELECT count(*) FROM autonomy_pending")),
            "runs": groups("SELECT status, count(*) FROM agent_runs GROUP BY status"),
            "tasks": groups("SELECT status, count(*) FROM tasks GROUP BY status"),
            "triggers": {
                "total": int(one("SELECT count(*) FROM proactive_triggers")),
                "active": int(one("SELECT count(*) FROM proactive_triggers WHERE is_active")),
                "next_run_at": _iso(one("SELECT min(next_run_at) FROM proactive_triggers WHERE is_active")),
            },
            "roles": {
                "total": int(one("SELECT count(*) FROM employee_roles")),
                "active": int(one("SELECT count(*) FROM employee_roles WHERE is_active")),
            },
            "usage": {
                "day_tokens": int(one("SELECT COALESCE(SUM(total_tokens), 0) FROM llm_usage "
                                      "WHERE created_at >= date_trunc('day', now())")),
                "month_tokens": int(one("SELECT COALESCE(SUM(total_tokens), 0) FROM llm_usage "
                                        "WHERE created_at >= date_trunc('month', now())")),
            },
        }
        cur.close()
    finally:
        conn.close()
    settings = autonomy.get_autonomy_settings()
    out["autonomy"] = {
        "mode": settings.get("mode"),
        "approval_channel": settings.get("approval_channel"),
        "approval_target_set": bool(settings.get("approval_target")),
        "channels": settings.get("channels") or [],
        "actions": settings.get("actions") or [],
    }
    return out


def autonomy_log(limit: int = 50, approved: Optional[bool] = None) -> List[Dict]:
    limit = max(1, min(int(limit), 200))
    tail = ""
    params: list = []
    if approved is not None:
        tail += " WHERE approved = %s"
        params.append(bool(approved))
    tail += " ORDER BY id DESC LIMIT %s"
    params.append(limit)
    keys = ("id", "action_type", "channel", "target", "content", "result", "approved", "created_at", "role",
            "decided_by", "decided_by_role", "decided_kind")
    conn = get_connection()
    try:
        cur = conn.cursor()
        try:
            cur.execute("SELECT id, action_type, channel, target, content, result, approved, created_at, role, "
                        "decided_by, decided_by_role, decided_kind FROM autonomy_log" + tail, params)
            rows = cur.fetchall()
        except Exception as e:
            if e.__class__.__name__ != "UndefinedColumn":
                raise
            conn.rollback()      # db/schema.sql is not applied yet: no "who" columns
            cur.execute("SELECT id, action_type, channel, target, content, result, approved, created_at, role "
                        "FROM autonomy_log" + tail, params)
            rows = [tuple(r) + (None, None, None) for r in cur.fetchall()]
        cur.close()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(zip(keys, r))
        d["created_at"] = _iso(d["created_at"])
        for k in ("target", "content", "result"):
            d[k] = (d[k] or "")[:MAX_LOG_TEXT]
        out.append(d)
    return out


def usage_summary() -> Dict:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT role, "
            "COALESCE(SUM(total_tokens) FILTER (WHERE created_at >= date_trunc('day', now())), 0), "
            "COALESCE(SUM(total_tokens) FILTER (WHERE created_at >= date_trunc('month', now())), 0), "
            "COUNT(*) FROM llm_usage GROUP BY role")
        used = {r[0]: (int(r[1]), int(r[2]), int(r[3])) for r in cur.fetchall()}
        cur.close()
    finally:
        conn.close()
    roles = employee_roles.list_roles()
    meta = {r["role"]: r for r in roles}
    names = [r["role"] for r in roles] + sorted(k for k in used if k and k not in meta)
    rows = []
    for name in names:
        day, month, calls = used.get(name, (0, 0, 0))
        lim = budget.limits(name)
        info = meta.get(name, {})
        rows.append({
            "role": name, "display_name": info.get("display_name") or name,
            "is_active": info.get("is_active", True), "sensitive": bool(is_sensitive_role(name)),
            "day_used": day, "month_used": month, "calls": calls,
            "day_limit": lim["day"], "month_limit": lim["month"],
        })
    g = budget.limits(None)
    un = used.get(None, (0, 0, 0))
    return {
        "global": {"day_used": sum(v[0] for v in used.values()), "month_used": sum(v[1] for v in used.values()),
                   "day_limit": g["day"], "month_limit": g["month"]},
        "unattributed": {"day_used": un[0], "month_used": un[1], "calls": un[2]},
        "roles": rows,
    }


# ---------------- org chart ----------------

REGULATED = "Regulated intake (sensitive)"
OTHER = "Other roles"
# Skill tags are too inconsistent to group by, so the departments are defined once, here.
# tests/test_office_org.py fails if a shipped role is missing, duplicated, or if the regulated
# group stops matching SENSITIVE_DOMAIN_ROLES.
DEPARTMENTS = [
    ("Leadership & admin", ["ceo", "manager", "secretary", "data_analyst"]),
    ("Sales & marketing", ["sales", "marketing", "content_writer", "social_media_manager",
                           "b2b_lead_qualifier", "franchise_inquiry_agent", "vehicle_sales_agent",
                           "real_estate_agent", "podcast_producer_agent"]),
    ("Customer service", ["support", "it_helpdesk", "returns_refunds_agent", "warranty_claims_agent",
                          "membership_agent", "admissions_agent", "property_management_agent"]),
    ("Bookings & appointments", ["hospitality_agent", "event_planner", "travel_agent", "fitness_studio_agent",
                                 "tutoring_agent", "auto_service_agent", "photography_booking_agent",
                                 "salon_spa_agent", "coworking_space_agent", "volunteer_coordinator"]),
    ("Operations & supply chain", ["operations", "procurement_agent", "inventory_agent", "logistics_agent",
                                   "warehouse_operations_agent", "delivery_dispatch_agent"]),
    ("Finance & people", ["finance", "collections_agent", "hr", "recruiter"]),
    (REGULATED, ["legal_intake", "healthcare_intake", "insurance_agent", "compliance_officer",
                 "veterinary_intake", "tax_preparation_intake", "immigration_intake"]),
]
_DEPT_OF = {role: name for name, roles in DEPARTMENTS for role in roles}
_ORDER = {role: i for i, role in enumerate(r for _n, roles in DEPARTMENTS for r in roles)}


def department_of(role: str, sensitive: bool) -> str:
    if role in _DEPT_OF:
        return _DEPT_OF[role]
    return REGULATED if sensitive else OTHER


def _iso_any(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def org_chart() -> Dict:
    usage = usage_summary()
    meta = {r["role"]: r for r in employee_roles.list_roles()}
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT assigned_role, count(*) FROM tasks WHERE assigned_role IS NOT NULL "
                    "AND status IN ('open', 'in_progress', 'blocked') GROUP BY 1")
        open_tasks = {r[0]: int(r[1]) for r in cur.fetchall()}
        cur.close()
    finally:
        conn.close()
    groups = {name: [] for name, _roles in DEPARTMENTS}
    groups[OTHER] = []
    for u in usage["roles"]:
        m = meta.get(u["role"], {})
        entry = {
            "role": u["role"], "display_name": u["display_name"], "is_active": u["is_active"],
            "sensitive": u["sensitive"], "channels": list(m.get("channels") or []),
            "skill_tags": list(m.get("skill_tags") or [])[:8],
            "skills_summary": (m.get("skills_summary") or "")[:400],
            "last_learned_at": _iso_any(m.get("last_learned_at")),
            "day_used": u["day_used"], "month_used": u["month_used"],
            "day_limit": u["day_limit"], "month_limit": u["month_limit"],
            "open_tasks": open_tasks.get(u["role"], 0),
        }
        groups[department_of(u["role"], u["sensitive"])].append(entry)
    for roles in groups.values():
        roles.sort(key=lambda e: (_ORDER.get(e["role"], 10 ** 6), e["role"]))
    settings = autonomy.get_autonomy_settings()
    return {
        "owner": {"mode": settings.get("mode")},
        "router": {"note": "Incoming messages are routed to the right employee automatically; a multi-part "
                           "request can be split across several employees."},
        "departments": [{"name": name, "roles": groups[name]} for name in [n for n, _r in DEPARTMENTS] + [OTHER]
                        if groups[name]],
    }
