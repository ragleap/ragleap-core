"""Roles and the route table (core/permissions.py). Every route must have its rule written down on purpose."""
import pytest

from core import permissions as perm


def test_roles_run_from_lowest_to_highest():
    assert perm.ROLES == ("viewer", "operator", "admin", "owner")


@pytest.mark.parametrize("have,need,ok", [
    ("viewer", "viewer", True), ("viewer", "operator", False), ("operator", "viewer", True),
    ("operator", "admin", False), ("admin", "operator", True), ("admin", "owner", False),
    ("owner", "owner", True), ("owner", "viewer", True),
    ("nobody", "viewer", False), ("owner", "nobody", False), ("", "viewer", False),
])
def test_allows(have, need, ok):
    assert perm.allows(have, need) is ok


@pytest.mark.parametrize("method,path,role", [
    ("GET", "/me", "viewer"), ("GET", "/overview", "viewer"), ("GET", "/documents", "viewer"),
    ("GET", "/tasks/abc-123", "operator"), ("POST", "/chat", "operator"), ("POST", "/upload", "operator"),
    ("POST", "/autonomy/pending/42/approve", "operator"), ("POST", "/autonomy/pending/42/reject", "operator"),
    ("GET", "/settings", "admin"), ("PUT", "/settings", "admin"), ("DELETE", "/documents/xyz", "admin"),
    ("POST", "/folder-ingest/approve", "admin"), ("POST", "/autonomy", "admin"),
    ("GET", "/users", "owner"), ("POST", "/users/abc/keys", "owner"), ("DELETE", "/users/a/keys/b", "owner"),
])
def test_known_rules(method, path, role):
    assert perm.required_role(method, path) == role


@pytest.mark.parametrize("method,path", [
    ("GET", "/something-new"), ("POST", "/overview"), ("DELETE", "/tasks/1"), ("GET", "/users/"),
    ("GET", "/employees/a/b/c"), ("GET", "/autonomy/pending/1/approve"), ("GET", ""), ("", "/me"),
    ("GET", "/documents/a%2Fb/../x"),
])
def test_anything_not_listed_needs_the_owner(method, path):
    assert perm.required_role(method, path) == "owner"


def test_method_is_part_of_the_rule():
    assert perm.required_role("get", "/documents") == "viewer"           # case does not matter
    assert perm.required_role("DELETE", "/documents") == "owner"         # only GET /documents is listed


def test_every_route_has_its_rule_or_is_public():
    from core import api
    public = lambda p: p in api.API_KEY_EXEMPT_PATHS or p.startswith(api.API_KEY_EXEMPT_PREFIXES)
    listed = {(m, t) for m, t, _ in perm.RULES}
    missing = []
    for route in api.app.routes:
        methods = getattr(route, "methods", None)
        if not methods or public(route.path):
            continue
        for m in sorted(methods - {"HEAD", "OPTIONS"}):
            if (m, route.path) not in listed:
                missing.append((m, route.path))
    assert not missing, "write a rule in core/permissions.py for: %s" % missing


def test_no_rule_points_at_a_route_that_does_not_exist():
    from core import api
    real = {(m, r.path) for r in api.app.routes if getattr(r, "methods", None) for m in r.methods}
    stale = [(m, t) for m, t, _ in perm.RULES if (m, t) not in real]
    assert not stale, stale


def test_public_routes_are_not_in_the_table():
    from core import api
    listed = {t for _, t, _ in perm.RULES}
    assert not (listed & api.API_KEY_EXEMPT_PATHS)
    assert not any(t.startswith(api.API_KEY_EXEMPT_PREFIXES) for t in listed)


def test_the_shared_key_and_the_open_api_count_as_the_owner():
    assert perm.BOOTSTRAP_ACTOR["role"] == "owner" and perm.OPEN_ACTOR["role"] == "owner"
