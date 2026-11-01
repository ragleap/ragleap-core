"""docs/single-sign-on.md stays in step with the code: every setting and route it names exists, and it is honest about what is
untested. The dashboard script keeps the places it must use for single sign-on."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = (ROOT / "docs" / "single-sign-on.md").read_text()
JS = (ROOT / "core" / "office_static" / "app.js").read_text()
CODE = "".join((ROOT / "core" / name).read_text() for name in ("oidc.py", "sso.py"))
API = (ROOT / "core" / "api.py").read_text()


def test_every_setting_in_the_guide_is_read_by_the_code():
    names = set(re.findall(r"`((?:OIDC|SSO)_[A-Z_]+)`", DOC))
    assert {"OIDC_ISSUER", "OIDC_REQUIRE_MFA", "SSO_SESSION_HOURS", "SSO_IDLE_MINUTES"} <= names
    for name in names:
        assert '"%s"' % name in CODE, name


def _norm(text):
    return re.sub(r"\{[a-z_]+\}", "{x}", text)


def test_every_route_in_the_guide_exists():
    found = re.findall(r"`(?:(?:GET|POST|DELETE) )?(/(?:users|auth)/[^`]*)`", DOC)
    assert "/users/{id}/identities" in found and "/users/{id}/sessions" in found
    for path in found:
        assert '"%s"' % _norm(path) in _norm(API), path


def test_the_guide_is_honest_about_what_is_not_tested_or_covered():
    lowered = DOC.lower()
    for needle in ("off by default", "fake provider only", "https", "personal key", "not covered yet", "saml"):
        assert needle in lowered, needle


def test_the_dashboard_keeps_its_single_sign_on_places():
    for needle in ("/auth/config", "/auth/logout", "#session=", "#sso_error=", "Sign in with SSO", "Sign out", "Register email",
                   "End sessions", "/identities", "/sessions"):
        assert needle in JS, needle
    assert re.search(r'startsWith\("/auth/"\)', JS)                       # only a path on this server is ever followed
    assert "localStorage" not in JS
