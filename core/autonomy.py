"""
Autonomous Loop for RagLeap Core.
Single-tenant port of production's api/autonomy_engine.py — no
workspace_id, plain SQL instead of Django ORM, no multi-tenant channel
senders (uses this repo's own channels/*/router.py functions instead).

Modes:
  off  - owner-initiated only (default)
  semi - AI proposes an action, owner approves via a "YES <id>" / "NO <id>"
         reply, action executes only on approval
  full - AI executes directly, logs everything

Two independent allowlist gates - allowed actions, allowed channels - are
checked before mode even matters, so even in "full" mode nothing fires
outside what the owner explicitly permitted. This mirrors production's
safety design exactly; see RFC discussion #171 for the full scoping.

Safety note: "semi" is the strongly recommended default for anything
acting on sensitive data (health, financial, legal contexts) until
there is a real track record - the approval step is the actual safety
mechanism here, not a formality.

Sensitive-role enforcement: if a caller passes a `role` that's listed
in core.employees.defaults.SENSITIVE_DOMAIN_ROLES, execute_or_request()
forces that single call down to "semi" even when the owner's general
mode is "full" -- this is the actual enforcement of the guardrail
documented in defaults.py's SENSITIVE_DOMAIN_ROLES comment, which
previously existed only as a convention nothing in code checked. It
only ever forces full->semi, never semi/off->something looser, and
role is optional throughout (existing callers with no role context
keep working exactly as before).
"""
import hmac
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from core.employees._db import get_connection
from core.employees import learning as employee_learning

logger = logging.getLogger(__name__)


def get_autonomy_settings() -> Dict:
    """Get current autonomy config. Single-tenant: one row, created on first access."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT mode, channels, actions, approval_channel, approval_target FROM autonomy_settings LIMIT 1"
        )
        row = cur.fetchone()
        if not row:
            cur.execute(
                "INSERT INTO autonomy_settings (mode, channels, actions, approval_channel, approval_target) "
                "VALUES ('off', '[]', '[]', 'telegram', '') "
                "RETURNING mode, channels, actions, approval_channel, approval_target"
            )
            row = cur.fetchone()
            conn.commit()
        cur.close()
        return {
            "mode": row[0],
            "channels": row[1] if isinstance(row[1], list) else json.loads(row[1] or "[]"),
            "actions": row[2] if isinstance(row[2], list) else json.loads(row[2] or "[]"),
            "approval_channel": row[3],
            "approval_target": row[4],
        }
    except Exception as e:
        logger.error(f"get_autonomy_settings error: {e}")
        return {"mode": "off", "channels": [], "actions": [], "approval_channel": "telegram", "approval_target": ""}
    finally:
        conn.close()


def set_autonomy(mode: str, channels: Optional[List[str]] = None,
                  actions: Optional[List[str]] = None,
                  approval_channel: str = "telegram",
                  approval_target: str = "") -> bool:
    """Update autonomy settings (single-tenant: updates the one row)."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM autonomy_settings LIMIT 1")
        row = cur.fetchone()
        if row:
            cur.execute(
                "UPDATE autonomy_settings SET mode=%s, channels=%s, actions=%s, "
                "approval_channel=%s, approval_target=%s, updated_at=now() WHERE id=%s",
                (mode, json.dumps(channels or []), json.dumps(actions or []),
                 approval_channel, approval_target, row[0]),
            )
        else:
            cur.execute(
                "INSERT INTO autonomy_settings (mode, channels, actions, approval_channel, approval_target) "
                "VALUES (%s, %s, %s, %s, %s)",
                (mode, json.dumps(channels or []), json.dumps(actions or []),
                 approval_channel, approval_target),
            )
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        logger.error(f"set_autonomy error: {e}")
        return False
    finally:
        conn.close()


def log_autonomous_action(action_type: str, channel: str, target: str,
                           content: str, result: str, approved: bool = True,
                           role: Optional[str] = None, decided_by: Optional[Dict] = None) -> None:
    """Log an autonomous action. Table keeps unbounded history; callers
    wanting production's "last 200" behavior should query with LIMIT.
    role is optional and nullable -- older callers/rows keep working.
    decided_by is the person (or the owner's chat) who approved or rejected it, as
    {"name", "role", "kind", "user_id"}; an action that ran on its own has none."""
    if action_type in ("list_files", "read_file"):
        # Keep only the fixed first line (size, truncation). The log never stores file contents.
        result = (result or "").split("\n", 1)[0]
    conn = get_connection()
    try:
        cur = conn.cursor()
        base = (action_type, channel, target, content[:2000], result[:2000], approved, role)
        if decided_by:
            try:
                cur.execute(
                    "INSERT INTO autonomy_log (action_type, channel, target, content, result, approved, role, "
                    "decided_by, decided_by_role, decided_by_user, decided_kind) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    base + (str(decided_by.get("name") or "")[:120] or None, decided_by.get("role"),
                            decided_by.get("user_id"), decided_by.get("kind")),
                )
            except Exception as e:
                if e.__class__.__name__ != "UndefinedColumn":
                    raise
                conn.rollback()      # db/schema.sql is not applied yet: keep the entry, without the person
                decided_by = None
        if not decided_by:
            cur.execute(
                "INSERT INTO autonomy_log (action_type, channel, target, content, result, approved, role) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)", base)
        conn.commit()
        cur.close()
    except Exception as e:
        logger.warning(f"log_autonomous_action error: {e}")
    finally:
        conn.close()


def _send_via_channel(channel: str, target: str, content: str) -> str:
    """
    Dispatch an approved/full-mode action to this repo's own single-tenant
    channel senders. Returns a human-readable result string; never raises.
    """
    try:
        if channel == "whatsapp":
            from channels.whatsapp.router import send_whatsapp_message
            ok = send_whatsapp_message(target, content)
            return f"WhatsApp to {target}: {'sent' if ok else 'failed'}"
        elif channel == "telegram":
            from channels.telegram.router import send_telegram_message
            ok = send_telegram_message(target, content)
            return f"Telegram to {target}: {'sent' if ok else 'failed'}"
        elif channel == "discord":
            from channels.discord.router import send_discord_message
            ok = send_discord_message(target, content)
            return f"Discord to {target}: {'sent' if ok else 'failed'}"
        elif channel == "webhook":
            from core.action_senders import send_webhook
            ok = send_webhook(target, content)
            return f"Webhook '{target}': {'sent' if ok else 'failed'}"
        elif channel == "slack":
            from core.action_senders import send_slack
            ok = send_slack(target, content)
            return f"Slack '{target}': {'sent' if ok else 'failed'}"
        elif channel == "email":
            from core.action_senders import send_email
            ok = send_email(target, content)
            return f"Email to {target}: {'sent' if ok else 'failed'}"
        elif channel == "crm":
            from core.action_senders import send_crm_update
            ok = send_crm_update(target, content)
            return f"CRM record update: {'updated' if ok else 'failed'}"
        elif channel == "calendar":
            from core.action_senders import send_calendar_event
            ok = send_calendar_event(target, content)
            return f"Calendar event: {'scheduled' if ok else 'failed'}"
        elif channel == "task":
            from core import tasks as core_tasks
            title = (content.splitlines()[0] if content else "Untitled task")[:200]
            t = core_tasks.create_task(title=title, description=content, creator=target or "employee")
            return f"Task created: {t['id']}"
        elif channel == "notify_owner":
            # target is ignored on purpose: notify_owner always goes to the
            # configured owner, the same approval_channel/approval_target
            # request_approval() uses -- a model-supplied target is never
            # trusted for this, it would defeat the point of "notify owner".
            settings = get_autonomy_settings()
            owner_ch = settings["approval_channel"]
            owner_target = settings["approval_target"]
            if not owner_target:
                return "notify_owner skipped: no approval_target configured"
            return _send_via_channel(owner_ch, owner_target, content)
        elif channel == "mcp":
            from core.mcp_client import call_tool
            return call_tool(target, content)
        elif channel == "code":
            from core.code_exec import run_code
            return run_code(content)
        elif channel == "shell":
            from core.code_exec import run_shell
            return run_shell(content)
        elif channel == "fetch":
            from core.page_fetch import fetch_page
            return fetch_page(target)
        elif channel == "files_list":
            from core.file_access import list_files
            return list_files(target, content)
        elif channel == "files_read":
            from core.file_access import read_file
            return read_file(target, content)
        elif channel == "files_request":
            from core.file_access import access_request_text
            return access_request_text(target)
        else:
            return f"Unsupported channel for autonomous send: {channel} (voice has no discrete send - see RFC #171)"
    except Exception as e:
        logger.error(f"Autonomous send failed on {channel}: {e}")
        return "Send error; see server logs."


def list_pending(limit: int = 50) -> List[Dict]:
    """Actions waiting for owner approval (semi mode), newest first, with full content."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT action_id, action_type, channel, target, content, subject, role, created_at "
            "FROM autonomy_pending ORDER BY created_at DESC LIMIT %s",
            (max(1, min(int(limit), 200)),),
        )
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()
    keys = ("action_id", "action_type", "channel", "target", "content", "subject", "role", "created_at")
    out = []
    for r in rows:
        d = dict(zip(keys, r))
        if d["created_at"] is not None:
            d["created_at"] = d["created_at"].isoformat()
        out.append(d)
    return out


def resolve_pending(action_id: str, approve: bool, decided_by: Optional[Dict] = None) -> Optional[str]:
    """
    Approve or reject one pending action through the SAME path as an owner chat
    reply ("YES/NO <id>"), so execution, logging and learning are identical.
    Returns the reply text, or None if no such pending action exists.
    """
    action_id = (action_id or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{8}", action_id):
        return None
    if not any(p["action_id"] == action_id for p in list_pending(200)):
        return None
    return process_approval_response(f"{'YES' if approve else 'NO'} {action_id}", decided_by=decided_by)


def _decision_instructions(action_id: str) -> str:
    """
    How the owner answers an approval request. APPROVAL_REPLIES=off (for deployments whose app
    cannot receive chat replies, such as a localhost-only install) points to the approval inbox
    instead of telling the owner to reply.
    """
    import os
    if os.environ.get("APPROVAL_REPLIES", "").strip().lower() == "off":
        return (
            "Replies here are not connected to the server. Approve or reject in the approval inbox:\n"
            f"POST /autonomy/pending/{action_id}/approve\n"
            f"POST /autonomy/pending/{action_id}/reject\n"
            "(GET /autonomy/pending lists everything waiting)"
        )
    return f'Reply "YES {action_id}" to approve\nReply "NO {action_id}" to reject'


def request_approval(action_type: str, channel: str, target: str,
                      content: str, action_id: str, role: Optional[str] = None) -> bool:
    """
    Send an approval request to the owner over the configured approval
    channel. Uses the same channel senders as _send_via_channel - approval
    requests themselves are just messages. role is shown to the owner
    when present so they know which AI Employee is asking.
    """
    settings = get_autonomy_settings()
    approval_ch = settings["approval_channel"]
    approval_target = settings["approval_target"]
    if not approval_target:
        logger.warning("No approval_target configured - cannot send approval request")
        return False

    role_line = f"Role: {role}\n" if role else ""
    msg = (
        f"Approval Request\n\n"
        f"Action: {action_type}\n"
        f"{role_line}"
        f"Channel: {channel}\n"
        f"Target: {target}\n\n"
        f"Message preview:\n{content[:300]}\n\n"
        f"{_decision_instructions(action_id)}"
    )
    result = _send_via_channel(approval_ch, approval_target, msg)
    return "sent" in result


def execute_or_request(action_type: str, channel: str, target: str,
                        content: str, execute_fn: Optional[Callable] = None,
                        subject: str = "", role: Optional[str] = None,
                        force_semi: bool = False) -> Dict:
    """
    Core autonomy dispatcher.
    - off mode: skip
    - allowlist gates (actions, channels) checked before mode matters
    - full mode: execute directly (via execute_fn if given, else the
      default channel sender), log
    - semi mode: store pending, request approval, wait for owner reply

    role (optional): when provided and present in
    core.employees.defaults.SENSITIVE_DOMAIN_ROLES, this single call is
    forced from "full" down to "semi" regardless of the owner's general
    autonomy mode -- the actual enforcement of the guardrail documented
    in defaults.py. Only ever tightens (full->semi), never loosens.

    Returns: {"status": "executed"|"pending_approval"|"skipped"|"error", ...}
    """
    settings = get_autonomy_settings()
    mode = settings["mode"]
    actions = settings["actions"]
    channels = settings["channels"]

    if mode == "off":
        return {"status": "skipped", "result": "autonomy off"}

    if actions and action_type not in actions:
        return {"status": "skipped", "result": f"{action_type} not in allowed actions"}

    if channels and channel not in channels:
        return {"status": "skipped", "result": f"{channel} not in allowed channels"}

    if mode == "full" and (force_semi or action_type == "request_access"):
        # caller (e.g. a run that has read untrusted web/MCP content) requires approval for this call
        logger.info(f"force_semi: {action_type} forced full->semi")
        mode = "semi"

    if mode == "full" and role:
        try:
            from core.employees.sensitivity import is_sensitive_role
            if is_sensitive_role(role):
                logger.info(
                    f"Sensitive-domain role '{role}' forced full->semi for "
                    f"action_type={action_type} (SENSITIVE_DOMAIN_ROLES guardrail)"
                )
                mode = "semi"
        except Exception as e:
            logger.warning(f"SENSITIVE_DOMAIN_ROLES check failed, proceeding with configured mode: {e}")

    if mode == "full":
        try:
            if execute_fn:
                result = execute_fn()
            else:
                result = _send_via_channel(channel, target, content)
            log_autonomous_action(action_type, channel, target, content, str(result), approved=True, role=role)
            return {"status": "executed", "result": result}
        except Exception as e:
            log_autonomous_action(action_type, channel, target, content, f"ERROR: {e}", approved=True, role=role)
            logger.error(f"Autonomous action {action_type} failed: {e}")
            return {"status": "error", "result": "action failed; see server logs"}

    elif mode == "semi":
        action_id = str(uuid.uuid4())[:8].upper()
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO autonomy_pending (action_id, action_type, channel, target, content, subject, role) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (action_id, action_type, channel, target, content, subject, role),
            )
            conn.commit()
            cur.close()
        except Exception as e:
            logger.warning(f"failed to store pending action: {e}")
        finally:
            conn.close()

        sent = request_approval(action_type, channel, target, content, action_id, role=role)
        return {"status": "pending_approval", "action_id": action_id, "approval_sent": sent}

    return {"status": "skipped", "result": "unknown mode"}


def _notify_agent_loop(action_id: str, approved: bool, result: str, action_type: str) -> None:
    """Let a waiting agent-loop run (if this pending action belongs to one) resume or end."""
    try:
        from core import agent_loop
        agent_loop.on_resolved(action_id, approved, result, action_type)
    except Exception as e:
        logger.warning(f"agent loop notification failed (non-fatal): {e}")


def process_approval_response(message: str, decided_by: Optional[Dict] = None) -> Optional[str]:
    """
    Process an owner reply of the form "YES ABC123" or "NO ABC123".
    Returns a human-readable reply, or None if the message isn't an
    approval response at all (caller should fall through to normal chat).
    """
    msg = message.strip().upper()
    m = re.match(r"^(YES|NO)\s+([A-Z0-9]{8})$", msg)
    if not m:
        return None

    approved = m.group(1) == "YES"
    action_id = m.group(2)

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT action_type, channel, target, content, subject, role FROM autonomy_pending WHERE action_id = %s",
            (action_id,),
        )
        row = cur.fetchone()
        if not row:
            cur.close()
            return f"Action {action_id} not found or already processed."

        action_type, channel, target, content, subject, role = row
        cur.execute("DELETE FROM autonomy_pending WHERE action_id = %s", (action_id,))
        conn.commit()
        cur.close()
    except Exception as e:
        logger.error(f"process_approval_response lookup error: {e}", exc_info=True)
        return "Error processing approval; see server logs."
    finally:
        conn.close()

    if not approved:
        log_autonomous_action(action_type, channel, target, content, "REJECTED by owner", approved=False, role=role,
                              decided_by=decided_by)
        employee_learning.learn_from_owner_approval(
            action_type, content, "REJECTED by owner", approved=False
        )
        _notify_agent_loop(action_id, False, "", action_type)
        return f"Action {action_id} rejected and cancelled."

    result = _send_via_channel(channel, target, content)
    log_autonomous_action(action_type, channel, target, content, result, approved=True, role=role,
                          decided_by=decided_by)
    employee_learning.learn_from_owner_approval(
        action_type, content, result, approved=True
    )
    _notify_agent_loop(action_id, True, result, action_type)
    return f"Action {action_id} approved and executed.\n{result}"


def _norm_sender(channel: str, value) -> str:
    """Comparable form of a sender id: whatsapp numbers lose the prefix, spaces, dashes and plus."""
    v = str(value if value is not None else "").strip()
    if channel == "whatsapp":
        v = v.replace("whatsapp:", "").replace(" ", "").replace("-", "").lstrip("+")
    return v.lower()


def is_owner_sender(channel: str, sender) -> bool:
    """True only when `sender` on `channel` is the owner configured as approval_channel /
    approval_target. Fails closed: no configured target, a different channel, or any error
    means False."""
    try:
        settings = get_autonomy_settings()
        target = _norm_sender(settings.get("approval_channel"), settings.get("approval_target"))
        if not target or settings.get("approval_channel") != channel:
            return False
        return hmac.compare_digest(_norm_sender(channel, sender).encode("utf-8"), target.encode("utf-8"))
    except Exception as e:
        logger.warning(f"Owner check failed (treating sender as not the owner): {e}")
        return False


def process_approval_from(channel: str, sender, message: str) -> Optional[str]:
    """Like process_approval_response, but only honours the reply when it comes from the
    configured owner. Anyone else gets None (the message falls through to normal chat), so a
    stranger cannot approve or reject actions, or learn whether an action id exists."""
    if not is_owner_sender(channel, sender):
        if re.match(r"^\s*(YES|NO)\s+[A-Za-z0-9]{8}\s*$", message or "", re.IGNORECASE):
            logger.warning(f"Approval-style message from a non-owner sender on {channel} ignored")
        return None
    return process_approval_response(message, decided_by={
        "name": f"the owner's {channel} chat", "role": "owner", "kind": "channel", "user_id": None})


def generate_autonomy_daily_report() -> str:
    """Generate a report of today's autonomous actions from autonomy_log."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT action_type, result FROM autonomy_log WHERE created_at >= CURRENT_DATE"
        )
        rows = cur.fetchall()
        cur.close()
    except Exception as e:
        logger.error(f"Autonomy report generation failed: {e}")
        return "Report generation error; see server logs."
    finally:
        conn.close()

    if not rows:
        return "Autonomy Report - No autonomous actions today."

    by_type: Dict[str, int] = {}
    failed = 0
    for action_type, result in rows:
        by_type[action_type] = by_type.get(action_type, 0) + 1
        if "ERROR" in str(result):
            failed += 1

    lines = [f"Autonomy Report - {datetime.now(timezone.utc).date().isoformat()}", ""]
    lines.append(f"Total actions: {len(rows)}")
    for t, count in by_type.items():
        lines.append(f"  - {t}: {count}")
    if failed:
        lines.append(f"\n{failed} failed actions - check logs")

    return "\n".join(lines)


def generate_rejection_pattern_report(min_days: int = 0) -> str:
    """
    Real, honest aggregation of owner rejections from autonomy_log -
    grouped by (action_type, channel, role), all-time (not just today,
    since generate_autonomy_daily_report() already covers today and
    rejections are rare enough that a daily window would almost always
    be empty). This is a report for a human to read, NOT a decision
    engine - it does not suggest or apply any config change itself.

    See suggest_autonomy_changes() below for why automated suggestions
    aren't built yet, and what real signal is needed before they can be.
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT action_type, channel, role FROM autonomy_log WHERE approved = false"
        )
        rows = cur.fetchall()
        cur.close()
    except Exception as e:
        logger.error(f"Rejection pattern report failed: {e}")
        return "Rejection pattern report error; see server logs."
    finally:
        conn.close()

    if not rows:
        return "Rejection Pattern Report - No rejections recorded yet."

    by_group: Dict[tuple, int] = {}
    for action_type, channel, role in rows:
        key = (action_type, channel, role or "(no role)")
        by_group[key] = by_group.get(key, 0) + 1

    lines = ["Rejection Pattern Report", "", f"Total rejections: {len(rows)}", ""]
    for (action_type, channel, role), count in sorted(by_group.items(), key=lambda kv: -kv[1]):
        lines.append(f"  - {action_type} via {channel} (role: {role}): {count} rejection(s)")

    return "\n".join(lines)


MIN_REJECTIONS_FOR_PATTERN_SUGGESTIONS = int(
    os.environ.get("MIN_REJECTIONS_FOR_PATTERN_SUGGESTIONS", "25")
)


def suggest_autonomy_changes() -> None:
    """
    Deliberately NOT implemented yet - this is the real, honest reason
    why, not a placeholder to quietly fill in later:

    Turning generate_rejection_pattern_report()'s raw counts into an
    actual suggestion ("action X via channel Y gets rejected often,
    consider removing it from your actions allowlist") requires a
    threshold for what counts as a real pattern versus noise. That
    threshold can only be chosen responsibly against real rejection
    volume - picking one now, with 1 total row in autonomy_log and 0
    real rejections at the time this was written, would mean guessing
    at a number with nothing to calibrate it against. A wrong threshold
    here is actively harmful: too low and it suggests locking down
    actions based on one or two unlucky misfires; too high and it never
    fires at all, giving false confidence that "no patterns" means
    "no problem."

    Before implementing this for real:
    1. Check actual volume: SELECT COUNT(*) FROM autonomy_log WHERE
       approved = false;
    2. Once MIN_REJECTIONS_FOR_PATTERN_SUGGESTIONS (default 25, override
       via env) real rejections exist, look at generate_rejection_pattern_
       report()'s actual output and choose a real per-group threshold
       informed by what real rejection clustering looks like - not a
       number invented in the abstract.
    3. This function should keep returning suggestions only (never apply
       a config change automatically) - the owner reviews and acts, same
       as every other autonomy decision in this codebase.
    """
    raise NotImplementedError(
        "Pattern-based suggestions need real rejection volume to "
        "calibrate against (see this function's docstring for why, and "
        "the exact query to check current volume). "
        "generate_rejection_pattern_report() is available now and shows "
        "the real, unfiltered counts - use that directly until this is "
        "implemented for real."
    )
