"""Read-only setup checklist for the AI Office Setup tab (GET /setup/status).

Returns only fixed sentences, provider and model names, and yes/no facts: never secrets, never
exception text. It makes no call to the AI provider (use the Test buttons in Settings for that);
the only network touch is a one-second TCP connect to the sandbox service when code or shell
tools are switched on.
"""
import logging
import os
import socket
from typing import Callable, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def _item(id_: str, title: str, state: str, detail: str, action: str = "",
          required: bool = False, fix: str = "") -> Dict:
    return {"id": id_, "title": title, "state": state, "required": required,
            "detail": detail, "action": action, "fix": fix}


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() == "true"


def _short(text, n: int = 80) -> str:
    s = str(text or "")
    return s if len(s) <= n else s[:n] + "..."


def _reachable(url: str, timeout: float = 1.0) -> bool:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return False
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        with socket.create_connection((parsed.hostname, port), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def _safe(id_: str, title: str, fn: Callable[[], Optional[Dict]], required: bool) -> Optional[Dict]:
    try:
        return fn()
    except Exception as e:
        logger.warning("Setup check %s failed: %s", id_, type(e).__name__)
        return _item(id_, title, "warn", "This check could not run.", "Look at the app logs.", required)


def _api_key(api_key_set: bool) -> Dict:
    if api_key_set:
        return _item("api_key", "API key", "ok", "The API is protected by a key.", required=True)
    return _item("api_key", "API key", "todo",
                 "No API key is set, so anyone who can reach this server can use it.",
                 "Set RAGLEAP_API_KEY in .env and restart.", True)


def _encryption() -> Dict:
    from core import settings
    if settings.encryption_ready():
        return _item("encryption", "Saving keys in the dashboard", "ok", "Keys you save in Settings are encrypted.")
    return _item("encryption", "Saving keys in the dashboard", "warn",
                 "Keys cannot be saved in the dashboard because ADDON_ENCRYPTION_KEY is not set. Keys in .env still work.",
                 "Add ADDON_ENCRYPTION_KEY to .env (the installer does this for you) and restart.")


def _chat_ai() -> Dict:
    from core import generation
    try:
        svc = generation.GenerationService()
    except ValueError:
        return _item("chat_ai", "Chat AI", "todo",
                     "The chat AI is not fully set up (a key, model or address is missing).",
                     "Open Settings, choose a provider, add the key and model, then press Test chat AI.",
                     True, "settings")
    cfg = svc.primary_config
    return _item("chat_ai", "Chat AI", "ok",
                 "Using " + _short(cfg.get("provider")) + " with model " + _short(cfg.get("model")) +
                 ". Press Test chat AI in Settings to confirm it answers.", required=True, fix="settings")


def _embeddings() -> Dict:
    from core import embedding, vector_dims
    try:
        svc = embedding.EmbeddingService()
    except ValueError:
        return _item("embeddings", "Document search (embeddings)", "todo",
                     "Document search is not fully set up (a key, model, address or vector size is missing).",
                     "Open Settings, choose an embedding provider and model, then press Test embeddings.",
                     True, "settings")
    dims = embedding.configured_dimensions()
    status = vector_dims.status(dims)
    bad = [s for s in status if s.get("status") == "mismatch"]
    if bad:
        t = bad[0]
        return _item("embeddings", "Document search (embeddings)", "todo",
                     "Your stored data in " + _short(t.get("table"), 40) + " uses " + str(t.get("have")) +
                     " dimensions but the setting says " + str(t.get("want")) + ", so search would break.",
                     "Switch the embedding settings back, or clear the stored documents and add them again.",
                     True, "settings")
    note = " The empty tables will be resized to match." if any(s.get("status") == "will_resize" for s in status) else ""
    return _item("embeddings", "Document search (embeddings)", "ok",
                 "Using " + _short(svc.provider) + " (" + _short(svc.model) + "), " + str(dims) + " dimensions." + note +
                 " Press Test embeddings in Settings to confirm.", required=True, fix="settings")


def _autonomy() -> Dict:
    from core import autonomy
    mode = autonomy.get_autonomy_settings().get("mode")
    if mode == "semi":
        return _item("autonomy", "Autonomy mode", "ok", "Semi mode: every action waits for your approval.")
    if mode == "off":
        return _item("autonomy", "Autonomy mode", "info",
                     "Autonomy is off: AI employees answer questions but never act on their own.",
                     "To let them act with your approval, set the mode to semi with POST /autonomy (a dashboard control is planned).")
    if mode == "full":
        return _item("autonomy", "Autonomy mode", "warn",
                     "Full mode: allowed actions run without asking (sensitive roles still need approval).",
                     "Use semi mode unless you are sure.")
    return _item("autonomy", "Autonomy mode", "warn", "The autonomy mode is not recognised.", "Set it with POST /autonomy.")


def _approvals() -> Dict:
    from core import autonomy
    s = autonomy.get_autonomy_settings()
    channel = s.get("approval_channel") or "telegram"
    target_set = bool(s.get("approval_target"))
    token_set = bool(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip())
    if target_set and (channel != "telegram" or token_set):
        return _item("approvals", "Approval ping", "ok", "You get a ping on " + _short(channel, 30) + " when an action needs approval.")
    if target_set:
        return _item("approvals", "Approval ping", "warn",
                     "An approval target is set but TELEGRAM_BOT_TOKEN is missing, so the ping cannot be sent.",
                     "Add TELEGRAM_BOT_TOKEN to .env and restart.")
    if s.get("mode") == "off":
        return _item("approvals", "Approval ping", "info",
                     "No approval ping is set up. It is only needed once autonomy is on.")
    return _item("approvals", "Approval ping", "warn",
                 "No approval ping is set up: pending actions appear only in the Approvals tab.",
                 "Set the approval channel and target with POST /autonomy and add TELEGRAM_BOT_TOKEN to .env.")


def _tools() -> Dict:
    code, shell = _flag("CODE_EXEC_ENABLED"), _flag("SHELL_EXEC_ENABLED")
    token = bool(os.environ.get("SANDBOX_TOKEN", "").strip())
    states = [("code", code), ("shell", shell), ("page fetch", _flag("BROWSER_FETCH_ENABLED")),
              ("agent loop", _flag("AGENT_LOOP_ENABLED")), ("task tool", _flag("ACTION_TASKS_ENABLED")),
              ("MCP", bool(os.environ.get("MCP_SERVERS", "").strip()))]
    on = [n for n, v in states if v]
    off = [n for n, v in states if not v]
    detail = "On: " + (", ".join(on) or "none") + ". Off: " + (", ".join(off) or "none") + "."
    if (code or shell) and not token:
        return _item("tools", "Tools", "warn",
                     detail + " Code and shell are switched on but SANDBOX_TOKEN is missing, so they stay off.",
                     "Add SANDBOX_TOKEN to .env and start the sandbox service.")
    return _item("tools", "Tools", "info", detail,
                 "Tool actions follow your autonomy mode. Switch tools on or off in .env.")


def _sandbox() -> Optional[Dict]:
    if not ((_flag("CODE_EXEC_ENABLED") or _flag("SHELL_EXEC_ENABLED")) and os.environ.get("SANDBOX_TOKEN", "").strip()):
        return None
    url = os.environ.get("SANDBOX_URL", "http://sandbox:8099").strip()
    if _reachable(url):
        return _item("sandbox", "Sandbox service", "ok", "The sandbox service answers.")
    return _item("sandbox", "Sandbox service", "warn", "Code or shell is on, but the sandbox service is not answering.",
                 "Start it with: docker compose --profile sandbox up -d sandbox")


def _budgets() -> Dict:
    from core import budget
    lim = budget.limits()
    role_caps = any(os.environ.get(n, "").strip() not in ("", "0") for n in (
        "BUDGET_ROLE_DAILY_TOKENS", "BUDGET_ROLE_MONTHLY_TOKENS",
        "BUDGET_ROLE_DAILY_OVERRIDES", "BUDGET_ROLE_MONTHLY_OVERRIDES"))
    if lim.get("day") or lim.get("month") or role_caps:
        return _item("budgets", "Token caps", "ok",
                     "Token caps are set (daily " + (str(lim.get("day")) if lim.get("day") else "none") +
                     ", monthly " + (str(lim.get("month")) if lim.get("month") else "none") + ").")
    return _item("budgets", "Token caps", "warn",
                 "No token caps are set, so a runaway agent could use unlimited tokens.",
                 "Optional: set BUDGET_DAILY_TOKENS and BUDGET_MONTHLY_TOKENS in .env.")


def _throttle() -> Dict:
    from core import auth_throttle
    if auth_throttle.enabled():
        return _item("throttle", "Login protection", "ok",
                     "After " + str(auth_throttle.max_failures()) + " wrong keys in " + str(auth_throttle.window_seconds()) +
                     " seconds, a client is locked out for " + str(auth_throttle.lockout_seconds()) + " seconds.")
    return _item("throttle", "Login protection", "warn", "Failed-login throttling is switched off.",
                 "Remove AUTH_THROTTLE=off from .env and restart.")


def _telegram() -> Dict:
    token = bool(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip())
    secret = bool(os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip())
    from channels.telegram import poller
    receiving = ("long polling (" + poller.status()["mode"] + ", " + ("answers anyone" if poller.public_answers() else "owner only") + ")") if poller.enabled() else "webhook only"
    return _item("telegram", "Telegram", "info",
                 "Bot token: " + ("set" if token else "not set") + ". Inbound webhook secret: " + ("set" if secret else "not set") + ". Receiving: " + receiving + ".",
                 "The token sends approval pings. Receiving Telegram messages needs a public HTTPS address for a webhook, or TELEGRAM_POLLING=true to ask Telegram for them instead (docs/telegram-polling.md).")


def build(api_key_set: bool) -> Dict:
    checks = [
        ("api_key", "API key", lambda: _api_key(api_key_set), True),
        ("encryption", "Saving keys in the dashboard", _encryption, False),
        ("chat_ai", "Chat AI", _chat_ai, True),
        ("embeddings", "Document search (embeddings)", _embeddings, True),
        ("autonomy", "Autonomy mode", _autonomy, False),
        ("approvals", "Approval ping", _approvals, False),
        ("tools", "Tools", _tools, False),
        ("sandbox", "Sandbox service", _sandbox, False),
        ("budgets", "Token caps", _budgets, False),
        ("throttle", "Login protection", _throttle, False),
        ("telegram", "Telegram", _telegram, False),
    ]
    items: List[Dict] = []
    for id_, title, fn, required in checks:
        item = _safe(id_, title, fn, required)
        if item:
            items.append(item)
    left = [i for i in items if i["required"] and i["state"] != "ok"]
    counts: Dict[str, int] = {}
    for i in items:
        counts[i["state"]] = counts.get(i["state"], 0) + 1
    summary = ("Everything required is set up." if not left
               else str(len(left)) + " required step" + ("" if len(left) == 1 else "s") + " left.")
    return {"ready": not left, "summary": summary, "counts": counts, "items": items}
