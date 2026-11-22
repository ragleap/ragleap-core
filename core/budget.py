"""
Usage ledger and token budgets (gap: per-agent budgets and cost caps).

Every LLM call made through GenerationService._call_provider() is recorded in the
llm_usage table: real provider-reported tokens, or an ESTIMATE (~4 characters per
token from the prompt and reply text) when the provider reports none, as with Ollama.
Caps are in TOKENS, so they never go stale like price tables do.

Caps (all off by default; 0 or unset = unlimited), counted in UTC days/months:
  BUDGET_DAILY_TOKENS, BUDGET_MONTHLY_TOKENS            all roles combined
  BUDGET_ROLE_DAILY_TOKENS, BUDGET_ROLE_MONTHLY_TOKENS  default cap per role
  BUDGET_ROLE_DAILY_OVERRIDES=support=50000,sales=0      per-role override (0 = unlimited)
  BUDGET_ROLE_MONTHLY_OVERRIDES=...                      same format
  USAGE_LEDGER=off                                       disable recording

check_budget() fails OPEN (a database problem never blocks answers: this is cost
control, not safety) and touches no database at all when no cap is configured.
Recording is best-effort and never raises.
"""
import contextvars
import logging
import os
from typing import Dict, List, Optional

from core.employees._db import get_connection

logger = logging.getLogger(__name__)

BUDGET_MESSAGE = "Sorry, the usage limit for this assistant has been reached for now. Please try again later."
WARN_FRACTION = 0.8

# Which role the current call chain is working for (set by core.chat.ask()).
_current_role = contextvars.ContextVar("ragleap_current_role", default=None)


def set_role(role) -> None:
    _current_role.set(str(role) if role else None)


def get_role() -> Optional[str]:
    return _current_role.get()


def estimate_tokens(text) -> int:
    n = len(text or "")
    return (n + 3) // 4 if n else 0


def _int_env(name: str) -> int:
    try:
        return max(0, int(float(os.environ.get(name, "0") or 0)))
    except (TypeError, ValueError):
        return 0


def _map_env(name: str) -> Dict[str, int]:
    out = {}
    for item in os.environ.get(name, "").split(","):
        if "=" not in item:
            continue
        key, _, val = item.partition("=")
        try:
            out[key.strip()] = max(0, int(float(val.strip())))
        except ValueError:
            continue
    return out


def _limits(role: Optional[str]) -> List[tuple]:
    """(scope, role_or_None, period, limit) for every configured cap that applies."""
    checks = []
    for period, env in (("day", "BUDGET_DAILY_TOKENS"), ("month", "BUDGET_MONTHLY_TOKENS")):
        limit = _int_env(env)
        if limit > 0:
            checks.append(("global", None, period, limit))
    if role:
        for period, default_env, override_env in (
            ("day", "BUDGET_ROLE_DAILY_TOKENS", "BUDGET_ROLE_DAILY_OVERRIDES"),
            ("month", "BUDGET_ROLE_MONTHLY_TOKENS", "BUDGET_ROLE_MONTHLY_OVERRIDES"),
        ):
            limit = _map_env(override_env).get(role, _int_env(default_env))
            if limit > 0:
                checks.append(("role", role, period, limit))
    return checks


def limits(role: Optional[str] = None) -> Dict[str, int]:
    """Configured caps for this scope only (global when role is None); 0 means no cap."""
    out = {"day": 0, "month": 0}
    for _scope, r, period, limit in _limits(role):
        if r == role:
            out[period] = limit
    return out


_SQL_USED = {
    ("day", False): "SELECT COALESCE(SUM(total_tokens), 0) FROM llm_usage WHERE created_at >= date_trunc('day', now())",
    ("month", False): "SELECT COALESCE(SUM(total_tokens), 0) FROM llm_usage WHERE created_at >= date_trunc('month', now())",
    ("day", True): "SELECT COALESCE(SUM(total_tokens), 0) FROM llm_usage WHERE created_at >= date_trunc('day', now()) AND role = %s",
    ("month", True): "SELECT COALESCE(SUM(total_tokens), 0) FROM llm_usage WHERE created_at >= date_trunc('month', now()) AND role = %s",
}


def _used(role: Optional[str], period: str) -> int:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(_SQL_USED[(period, bool(role))], (role,) if role else ())
        value = int(cur.fetchone()[0] or 0)
        cur.close()
        return value
    finally:
        conn.close()


def remaining(role: Optional[str]) -> Optional[int]:
    """Tokens left under this role's OWN caps (the smallest across day and month), or None when it has none. A global cap
    is not counted here. Fails OPEN (None), like check_budget."""
    try:
        left = [limit - _used(r, period) for scope, r, period, limit in _limits(role) if scope == "role"]
        return min(left) if left else None
    except Exception as e:
        logger.warning(f"Budget remaining check failed (treating as no cap): {e}")
        return None


def check_budget(role: Optional[str] = None) -> Optional[Dict]:
    """None if the call may proceed, else {"scope", "role", "period", "limit", "used"}."""
    try:
        checks = _limits(role)
        if not checks:
            return None
        for scope, r, period, limit in checks:
            used = _used(r, period)
            if used >= limit:
                return {"scope": scope, "role": r, "period": period, "limit": limit, "used": used}
            if used >= limit * WARN_FRACTION:
                logger.warning(f"Budget warning: {scope} {period} usage {used}/{limit} tokens"
                               + (f" (role {r})" if r else ""))
        return None
    except Exception as e:
        logger.warning(f"Budget check failed (allowing the call): {e}")
        return None


def record_usage(provider, model, prompt, text, usage) -> None:
    """Write one ledger row. Uses provider-reported tokens when present, else estimates."""
    try:
        if os.environ.get("USAGE_LEDGER", "").strip().lower() == "off":
            return
        usage = usage or {}
        prompt_tokens, completion_tokens = usage.get("prompt_tokens"), usage.get("completion_tokens")
        estimated = False
        if prompt_tokens is None:
            prompt_tokens, estimated = estimate_tokens(prompt), True
        if completion_tokens is None:
            completion_tokens, estimated = estimate_tokens(text), True
        prompt_tokens, completion_tokens = int(prompt_tokens), int(completion_tokens)
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO llm_usage (role, provider, model, prompt_tokens, completion_tokens, "
                "total_tokens, estimated) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (get_role(), provider, model, prompt_tokens, completion_tokens,
                 prompt_tokens + completion_tokens, estimated),
            )
            conn.commit()
            cur.close()
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"Usage recording failed (non-fatal): {e}")
