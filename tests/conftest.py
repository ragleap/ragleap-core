"""
Test-suite safety guard: refuse to run against a non-test database.

Several tests create/delete rows and rewrite autonomy settings, so running
the suite against a live database pollutes it (this happened: a test's
escalation row and an autonomy_settings update landed in production).
The database name must end in "_test". The check is skipped in CI (the
CI variable GitHub Actions always sets; its databases are throwaway
service containers) or with RAGLEAP_ALLOW_ANY_DB=1.
"""
import os
from typing import Optional

import pytest


# tests never wait between provider retries
os.environ.setdefault("ACTION_RETRY_DELAY", "0")


def database_guard_error(url: Optional[str], env=None) -> Optional[str]:
    env = os.environ if env is None else env
    if env.get("CI") or env.get("RAGLEAP_ALLOW_ANY_DB") == "1":
        return None
    url = (url or "").strip()
    if not url:
        return None
    name = url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    if name.endswith("_test"):
        return None
    return (
        f"Refusing to run the test suite against database '{name}': tests create and "
        "delete real rows and rewrite autonomy settings. Point DATABASE_URL at a "
        "database whose name ends in _test (for example ragleap_core_test). "
        "CI and RAGLEAP_ALLOW_ANY_DB=1 skip this check."
    )


def pytest_sessionstart(session):
    msg = database_guard_error(os.environ.get("DATABASE_URL"))
    if msg:
        pytest.exit(msg, returncode=3)


# Opt-in feature flags that a developer may have in .env or the shell. Tests must see the
# documented defaults (everything off); a test that needs a flag sets it with monkeypatch.
_FEATURE_FLAGS = (
    "AGENT_LOOP_ENABLED", "AGENT_LOOP_MAX_STEPS", "CODE_EXEC_ENABLED", "SHELL_EXEC_ENABLED",
    "SANDBOX_TOKEN", "SANDBOX_URL", "BROWSER_FETCH_ENABLED", "BROWSER_ALLOWED_DOMAINS",
    "MCP_SERVERS", "MCP_ALLOWED_TOOLS", "ACTION_TASKS_ENABLED", "APPROVAL_REPLIES",
    "BUDGET_DAILY_TOKENS", "BUDGET_MONTHLY_TOKENS", "BUDGET_ROLE_DAILY_TOKENS",
    "BUDGET_ROLE_MONTHLY_TOKENS", "BUDGET_ROLE_DAILY_OVERRIDES", "BUDGET_ROLE_MONTHLY_OVERRIDES",
    "EMBEDDING_PROVIDER", "EMBEDDING_DIMENSIONS", "EMBEDDING_BATCH_SIZE", "OLLAMA_EMBEDDING_MODEL",
    "OPENAI_EMBEDDING_MODEL", "MISTRAL_EMBEDDING_MODEL", "CUSTOM_EMBEDDING_MODEL",
    "AUTH_THROTTLE", "AUTH_MAX_FAILURES", "AUTH_WINDOW_SECONDS", "AUTH_LOCKOUT_SECONDS", "TRUSTED_PROXIES", "LANGUAGE_DETECTION_MODEL",
    "FILE_TOOLS_ENABLED", "SCHEDULER_ENABLED",
    "FOLDER_INGEST_ENABLED", "FOLDER_INGEST_INTERVAL_MINUTES", "FOLDER_INGEST_MAX_FILES", "FOLDER_INGEST_DELETE_REMOVED",
    "FOLDER_INGEST_AUTO_APPROVE", "FOLDER_INGEST_STAGING_DIR", "SHARED_KEY_DISABLED",
)


@pytest.fixture(autouse=True)
def _feature_flags_off_by_default(monkeypatch):
    for name in _FEATURE_FLAGS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _reset_auth_throttle():
    from core import auth_throttle
    auth_throttle.reset()
    yield
    auth_throttle.reset()
