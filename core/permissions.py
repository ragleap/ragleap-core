"""
Roles, and the one table that says who may call what.

Four roles, lowest to highest: viewer, operator, admin, owner. A route's rule names the LOWEST role that may call it.
Anything not listed here needs the owner (deny by default), so a new route is never open by accident; a test fails
until its rule is written down on purpose. Public routes (health, the dashboard files, the platform webhooks) are not
listed: the API key middleware lets them through before any role is looked at.

The old shared RAGLEAP_API_KEY counts as the owner. With no RAGLEAP_API_KEY set the API is open, as before.
"""
import re

ROLES = ("viewer", "operator", "admin", "owner")
_RANK = {role: i for i, role in enumerate(ROLES)}

BOOTSTRAP_ACTOR = {"kind": "bootstrap", "name": "Owner (shared key)", "role": "owner", "user_id": None}
OPEN_ACTOR = {"kind": "open", "name": "No authentication (open API)", "role": "owner", "user_id": None}

# (method, route template, lowest role allowed)
RULES = (
    # Anyone signed in: overview-level pages and read-only lists of names and numbers
    ("GET", "/me", "viewer"),
    ("GET", "/overview", "viewer"),
    ("GET", "/org", "viewer"),
    ("GET", "/usage/summary", "viewer"),
    ("GET", "/setup/status", "viewer"),
    ("GET", "/employees", "viewer"),
    ("GET", "/documents", "viewer"),
    ("GET", "/folder-ingest", "viewer"),
    ("GET", "/file-grants", "viewer"),
    ("GET", "/proactive-triggers", "viewer"),
    ("GET", "/proactive-triggers/{trigger_id}", "viewer"),
    ("GET", "/observability/report", "viewer"),
    ("GET", "/autonomy", "viewer"),
    ("GET", "/autonomy/report", "viewer"),
    ("GET", "/autonomy/rejection-report", "viewer"),
    ("GET", "/docs", "viewer"),
    ("GET", "/docs/oauth2-redirect", "viewer"),
    ("GET", "/redoc", "viewer"),
    ("GET", "/openapi.json", "viewer"),
    # Operators do the daily work: see what the AI did and proposed, ask, upload, decide on actions
    ("GET", "/autonomy/pending", "operator"),
    ("GET", "/autonomy/log", "operator"),
    ("GET", "/agent-runs", "operator"),
    ("GET", "/agent-runs/{run_id}", "operator"),
    ("GET", "/employees/{role}", "operator"),
    ("GET", "/employees/{role}/context", "operator"),
    ("GET", "/tasks", "operator"),
    ("GET", "/tasks/{task_id}", "operator"),
    ("GET", "/profile", "operator"),
    ("GET", "/integrations", "operator"),
    ("GET", "/n8n-workflows", "operator"),
    ("GET", "/n8n-workflows/{workflow_id}", "operator"),
    ("GET", "/channels/{channel}/role", "operator"),
    ("POST", "/chat", "operator"),
    ("POST", "/chat/stream", "operator"),
    ("POST", "/chat/feedback", "operator"),
    ("POST", "/upload", "operator"),
    ("POST", "/ingest/url", "operator"),
    ("POST", "/tasks", "operator"),
    ("PATCH", "/tasks/{task_id}", "operator"),
    ("POST", "/profile/learn", "operator"),
    ("POST", "/autonomy/execute", "operator"),
    ("POST", "/autonomy/pending/{action_id}/approve", "operator"),
    ("POST", "/autonomy/pending/{action_id}/reject", "operator"),
    # Admins change how the system is set up and what it may spend
    ("GET", "/audit", "admin"),
    ("GET", "/metrics", "admin"),
    ("GET", "/settings", "admin"),
    ("PUT", "/settings", "admin"),
    ("POST", "/settings/test/embedding", "admin"),
    ("POST", "/settings/test/llm", "admin"),
    ("POST", "/autonomy", "admin"),
    ("POST", "/channels/{channel}/role", "admin"),
    ("PATCH", "/employees/{role}", "admin"),
    ("PATCH", "/profile", "admin"),
    ("DELETE", "/documents/{document_id}", "admin"),
    ("POST", "/folder-ingest/approve", "admin"),
    ("POST", "/proactive-triggers", "admin"),
    ("PATCH", "/proactive-triggers/{trigger_id}", "admin"),
    ("DELETE", "/proactive-triggers/{trigger_id}", "admin"),
    ("POST", "/integrations", "admin"),
    ("POST", "/integrations/csv", "admin"),
    ("POST", "/integrations/{data_source_id}/sync", "admin"),
    ("POST", "/integrations/{data_source_id}/test", "admin"),
    ("POST", "/n8n-workflows", "admin"),
    ("PATCH", "/n8n-workflows/{workflow_id}", "admin"),
    ("DELETE", "/n8n-workflows/{workflow_id}", "admin"),
    # Only the owner manages people and keys
    ("GET", "/users", "owner"),
    ("POST", "/users", "owner"),
    ("PATCH", "/users/{user_id}", "owner"),
    ("POST", "/users/{user_id}/keys", "owner"),
    ("DELETE", "/users/{user_id}/keys/{key_id}", "owner"),
    ("GET", "/users/{user_id}/channels", "owner"),
    ("POST", "/users/{user_id}/channels", "owner"),
    ("DELETE", "/users/{user_id}/channels/{link_id}", "owner"),
    ("GET", "/users/{user_id}/identities", "owner"),
    ("POST", "/users/{user_id}/identities", "owner"),
    ("DELETE", "/users/{user_id}/identities/{identity_id}", "owner"),
    ("DELETE", "/users/{user_id}/sessions", "owner"),
    ("POST", "/auth/logout", "viewer"),
)

for _method, _template, _role in RULES:
    assert _role in _RANK, (_method, _template, _role)


def _regex(template: str):
    parts = re.split(r"(\{[^/{}]+\})", template)
    return re.compile("".join("[^/]+" if p.startswith("{") else re.escape(p) for p in parts))


_COMPILED = tuple((method, _regex(template), role) for method, template, role in RULES)


def required_role(method: str, path: str) -> str:
    """The lowest role allowed to call this request. Unknown requests need the owner."""
    method = (method or "").upper()
    for rule_method, pattern, role in _COMPILED:
        if rule_method == method and pattern.fullmatch(path or ""):
            return role
    return "owner"


def allows(have: str, need: str) -> bool:
    return _RANK.get(have, -1) >= _RANK.get(need, len(ROLES))
