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


def sha256_text(text: str) -> str:
    import hashlib
    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()


def _audit_event(event: str, **fields) -> None:
    """Emit one structured audit log line (core/audit.emit). Never raises."""
    try:
        from core import audit
        audit.emit(event, **fields)
    except Exception:
        pass


def log_autonomous_action(action_type: str, channel: str, target: str,
                           content: str, result: str, approved: bool = True,
                           role: Optional[str] = None, decided_by: Optional[Dict] = None,
                           action_id: Optional[str] = None) -> None:
    """Log an autonomous action. Table keeps unbounded history; callers
    wanting production's "last 200" behavior should query with LIMIT.
    role is optional and nullable -- older callers/rows keep working.
    decided_by is the person (or the owner's chat) who approved or rejected it, as
    {"name", "role", "kind", "user_id"}; an action that ran on its own has none."""
    if action_type in ("list_files", "read_file"):
        # Keep only the fixed first line (size, truncation). The log never stores file contents.
        result = (result or "").split("\n", 1)[0]
    _r = result or ""
    _outcome = "rejected" if not approved else ("error" if _r.startswith("ERROR") else "executed")
    _d = decided_by or {}
    _audit_event("action_" + _outcome, content=content, action_id=action_id, action_type=action_type,
                 channel=channel, target=target, role=role, outcome=_outcome,
                 autonomous=bool(approved and not decided_by), approver=_d.get("name"),
                 approver_role=_d.get("role"), approver_kind=_d.get("kind"), result_sha256=sha256_text(_r))
    conn = get_connection()
    try:
        cur = conn.cursor()
        base = (action_type, channel, target, _log_content(channel, content)[:2000], result[:2000], approved, role)
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
        elif channel == "files_preview":
            from core.file_access import preview_change
            return preview_change(target, content)
        elif channel == "files_write":
            from core.file_access import write_file
            return write_file(target, content)
        elif channel == "files_delete":
            from core.file_access import delete_file
            return delete_file(target, content)
        elif channel == "files_request":
            from core.file_access import access_request_text
            return access_request_text(target)
        else:
            return f"Unsupported channel for autonomous send: {channel} (voice has no discrete send - see RFC #171)"
    except Exception as e:
        logger.error(f"Autonomous send failed on {channel}: {e}")
        return "Send error; see server logs."


def _write_bundle(content: str):
    """The bound JSON of a write_file action as a dict, or None when content is not that."""
    try:
        d = json.loads(content)
    except Exception:
        return None
    return d if isinstance(d, dict) and isinstance(d.get("path"), str) and isinstance(d.get("new"), str) else None


def _approval_preview(channel: str, content: str, limit: int = 300) -> str:
    """What a person reads before approving. Normal channels: the start of the content. A file write: grant-less
    summary with path, what it replaces, lines changed, short hashes and the start of the real diff."""
    if channel == "files_delete":
        dd = _delete_bundle(content)
        if dd is not None:
            head = ["Delete file: %s (it is moved to the trash, not erased; the owner can put it back)" % str(dd.get("path"))[:200],
                    "%s bytes, sha256 %s" % (int(dd.get("size") or 0), str(dd.get("sha"))[:16])]
            if dd.get("binary"):
                return "\n".join(head) + "\n(a binary file; its content is not shown)"
            return "\n".join(head) + "\n--- the start of the file ---\n" + str(dd.get("head") or "")[:300]
    d = _write_bundle(content) if channel == "files_write" else None
    if d is None:
        return content[:limit]
    base = str(d.get("base") or "-")
    kind = "replaces an existing file" if base != "-" else "creates a new file"
    head = [f"Write file: {str(d.get('path'))[:200]} ({kind})",
            f"{int(d.get('added') or 0)} lines added, {int(d.get('removed') or 0)} removed",
            f"old sha256 {base[:16]}, new sha256 {str(d.get('new'))[:16]}",
            "--- the change (diff, start) ---"]
    return "\n".join(head) + "\n" + str(d.get("diff") or "")[:800]


def _log_content(channel: str, content: str) -> str:
    """What the action log and the learning store keep. A file write keeps its path and hashes, never the text."""
    if channel == "files_delete":
        dd = _delete_bundle(content)
        if dd is not None:
            return f"delete_file {str(dd.get('path'))[:200]} sha={str(dd.get('sha'))[:16]}"
    d = _write_bundle(content) if channel == "files_write" else None
    if d is None:
        return content
    return f"write_file {str(d.get('path'))[:200]} base={str(d.get('base'))[:16]} new={str(d.get('new'))[:16]}"


def _bind_file_write(target: str, content: str):
    """Bind a proposed write to the real preview. content is the path on line 1 and the whole new text after it.
    Returns (bound JSON string, None) or (None, why not). The runner is only asked for a read-only preview."""
    from core import file_access
    first, _, text = (content or "").partition("\n")
    prev, why = file_access.preview_raw(target, first.strip(), text)
    if prev is None:
        return None, f"write_file refused: {why}"
    if prev.get("identical"):
        return None, "write_file: the new text is identical to the file, nothing to change"
    bound = {"path": first.strip(), "base": prev.get("old_sha256") or "-", "new": prev["new_sha256"], "text": text,
             "exists": bool(prev.get("exists")), "added": int(prev.get("added") or 0), "removed": int(prev.get("removed") or 0),
             "diff": str(prev.get("diff") or "")}
    return json.dumps(bound), None


def _delete_bundle(content: str):
    """The bound JSON of a delete_file action as a dict, or None when content is not that."""
    try:
        d = json.loads(content)
    except Exception:
        return None
    return d if isinstance(d, dict) and isinstance(d.get("path"), str) and isinstance(d.get("sha"), str) else None


def _bind_file_delete(target: str, content: str):
    """Bind a proposed delete to what the runner reports about the file now (read-only). content is the path.
    Returns (bound JSON string, None) or (None, why not)."""
    from core import file_access
    path = (content or "").strip()
    st, why = file_access.stat_raw(target, path)
    if st is None:
        return None, f"delete_file refused: {why}"
    bound = {"path": path, "sha": st["sha256"], "size": int(st["size"]), "binary": bool(st.get("binary")),
             "head": "" if st.get("binary") else str(st.get("head") or "")[:300]}
    return json.dumps(bound), None


def list_pending(limit: int = 50) -> List[Dict]:
    """Actions waiting for owner approval (semi mode), newest first, with full content."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT action_id, action_type, channel, target, content, subject, role, created_at, action_hash "
            "FROM autonomy_pending ORDER BY created_at DESC LIMIT %s",
            (max(1, min(int(limit), 200)),),
        )
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()
    keys = ("action_id", "action_type", "channel", "target", "content", "subject", "role", "created_at", "action_hash")
    out = []
    for r in rows:
        d = dict(zip(keys, r))
        d["action_hash"] = d.get("action_hash")
        d["ref"] = (d["action_hash"] or "")[:12] or None
        if d["created_at"] is not None:
            d["created_at"] = d["created_at"].isoformat()
        d["display"] = _approval_preview(d["channel"], d["content"], limit=10**6)
        out.append(d)
    return out


def resolve_pending(action_id: str, approve: bool, decided_by: Optional[Dict] = None, action_hash: Optional[str] = None) -> Optional[str]:
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
    if action_hash is not None and not re.fullmatch(r"[0-9a-fA-F]{12,64}", str(action_hash).strip()):
        return f"Action {action_id} was not run: the reference must be 12 to 64 hex characters (the Ref shown with the action)."
    return process_approval_response(f"{'YES' if approve else 'NO'} {action_id}", decided_by=decided_by, expected_hash=action_hash)


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


def action_hash(action_id, action_type, channel, target, content, subject, role) -> str:
    """SHA-256 of the canonical form of an action: id, type, channel, target, content, subject and role. Changing even one
    character of any of them changes the hash (P1-A)."""
    import hashlib

    def s(v):
        return "" if v is None else str(v)
    body = json.dumps({"v": 1, "id": s(action_id), "type": s(action_type), "channel": s(channel), "target": s(target),
                       "content": s(content), "subject": s(subject), "role": None if role is None else str(role)},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8", "surrogatepass")).hexdigest()


def policy_digest(settings: Optional[Dict] = None) -> str:
    """SHA-256 of the autonomy settings an approval is given under: mode, allowed actions, allowed channels and the approval
    chat. It is stored (and signed) with a pending action and compared again right before the action runs, so an approval
    given under one policy cannot run under another. (When the capability registry is wired into the gate, its generation
    joins this digest.) get_autonomy_settings falls back to the safe default when the database fails, which also changes
    the digest, so a failure refuses the action."""
    import hashlib
    s = settings if settings is not None else get_autonomy_settings()
    body = json.dumps({"v": 1, "mode": s.get("mode"), "actions": sorted(str(a) for a in (s.get("actions") or [])),
                       "channels": sorted(str(c) for c in (s.get("channels") or [])),
                       "approval_channel": s.get("approval_channel"), "approval_target": s.get("approval_target")},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8", "surrogatepass")).hexdigest()


def _approval_mac(key: bytes, key_id, action_id, ahash, policy) -> str:
    import hashlib
    msg = "|".join(("approval2", str(key_id), str(action_id), str(ahash), str(policy)))
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).hexdigest()


def approval_binding(action_id, action_type, channel, target, content, subject, role, policy: Optional[str] = None):
    """(hash, signature) stored with a proposed action; the signature covers the hash AND the policy digest. The signature is
    "<key id>:<hmac>", or None when no signing key is configured (then only the hash is stored, which catches a changed
    field but not a rewritten hash)."""
    h = action_hash(action_id, action_type, channel, target, content, subject, role)
    policy = policy or policy_digest()
    key_id, keys = _signing_keys()
    if key_id is None:
        return h, None
    return h, f"{key_id}:{_approval_mac(keys[key_id], key_id, action_id, h, policy)}"


def verify_approval(action_id, action_type, channel, target, content, subject, role, stored_hash, stored_sig,
                    stored_policy=None, current_policy=None):
    """(ok, reason): does the exact action about to run still match what was proposed, under the policy it was approved
    under? A row without a hash or without a policy digest (made before the binding existed, or written by hand) is refused,
    never run. Fails closed on any error."""
    try:
        if not stored_hash:
            return False, "unbound_refused"
        if not stored_policy:
            return False, "unbound_policy_refused"
        if not hmac.compare_digest(action_hash(action_id, action_type, channel, target, content, subject, role), str(stored_hash)):
            return False, "hash_mismatch"
        key_id, keys = _signing_keys()
        if stored_sig:
            kid, _, mac = str(stored_sig).partition(":")
            key = keys.get(kid)
            if key is None:
                return False, "unknown_key"
            if not hmac.compare_digest(_approval_mac(key, kid, action_id, str(stored_hash), str(stored_policy)), mac):
                return False, "bad_signature"
            ok_reason = "ok"
        elif key_id is not None:
            return False, "signature_missing"      # a key is configured now: an unsigned row could be a downgrade
        else:
            ok_reason = "ok_hash_only"
        now = current_policy if current_policy is not None else policy_digest()
        if not hmac.compare_digest(str(now), str(stored_policy)):
            return False, "policy_changed"          # the autonomy settings are not the ones it was approved under
        return True, ok_reason
    except Exception as e:
        logger.warning("Approval check failed, refusing: %s", e)
        return False, "check_failed"


_REFUSAL_TEXT = {
    "hash_mismatch": "it does not match what was proposed",
    "policy_changed": "your autonomy settings changed after it was proposed",
    "unbound_refused": "it was created before approvals were bound to the exact action",
    "unbound_policy_refused": "it was created before approvals were bound to your autonomy settings",
}


def _execute_approved(action_id: str, fields: Dict, stored_hash, stored_sig, stored_policy=None):
    """THE last step before an approved action runs: recompute the hash of the exact fields about to be executed and compare
    it with the one the owner approved, and check that the autonomy settings are still the ones it was approved under.
    Returns (True, result) or (False, reason); on False nothing was sent."""
    ok, why = verify_approval(action_id, fields.get("action_type"), fields.get("channel"), fields.get("target"), fields.get("content"),
                              fields.get("subject"), fields.get("role"), stored_hash, stored_sig, stored_policy)
    if not ok:
        _audit_event("approval_mismatch", action_id=action_id, action_type=fields.get("action_type"), channel=fields.get("channel"), reason=why)
        return False, why
    return True, _send_via_channel(fields["channel"], fields["target"], fields["content"])


def request_approval(action_type: str, channel: str, target: str,
                      content: str, action_id: str, role: Optional[str] = None, ref: Optional[str] = None) -> bool:
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
    ref_line = f"Ref: {str(ref)[:12]}\n" if ref else ""
    msg = (
        f"Approval Request\n\n"
        f"Action: {action_type}\n"
        f"{role_line}"
        f"Channel: {channel}\n"
        f"Target: {target}\n"
        f"{ref_line}\n"
        f"Message preview:\n{_approval_preview(channel, content)}\n\n"
        f"{_decision_instructions(action_id)}"
    )
    result = _send_via_channel(approval_ch, approval_target, msg)
    return "sent" in result


# File tools: the label (action_type) and the effect channel must always travel together.
_FILE_CHANNEL_FOR_ACTION = {"write_file": "files_write", "delete_file": "files_delete", "request_access": "files_request"}
_FORCED_ACTION_TYPES = tuple(_FILE_CHANNEL_FOR_ACTION)
_FORCED_CHANNELS = tuple(_FILE_CHANNEL_FOR_ACTION.values())


def file_pair_error(action_type: str, channel: str) -> Optional[str]:
    """Why this (action_type, channel) pair is refused, or None. A file label needs its own file channel and a
    file channel needs its own label, so a harmless label cannot carry a file effect past forced approval."""
    want = _FILE_CHANNEL_FOR_ACTION.get(action_type)
    if want is not None and channel != want:
        return f"action_type {action_type} must use channel {want}"
    if want is None and channel in _FORCED_CHANNELS:
        return f"channel {channel} needs its own file action_type"
    return None


def _role_is_sensitive(role) -> bool:
    from core.employees.sensitivity import is_sensitive_role
    return bool(role) and bool(is_sensitive_role(role))


def _live_chain_sensitive(run: Dict) -> bool:
    """True when a run above this one is a sensitive role, read from the run table each time. A missing, unreadable, looping
    or very long chain counts as sensitive."""
    try:
        from core import agent_loop
        seen, current = set(), run
        for _ in range(8):
            pid = current.get("parent_id")
            if not pid:
                return False
            if pid in seen:
                return True
            seen.add(pid)
            parent = agent_loop.get_run(pid)
            # A missing parent is refused HERE on purpose. Do not fold this into the check below or rely on the exception
            # handler: without this line current=None would raise and still fail closed, but only by accident.
            if parent is None:
                return True
            if _role_is_sensitive(parent.get("role")):
                return True
            current = parent
        return True
    except Exception as e:
        logger.warning("Sub-agent inheritance check failed, forcing approval: %s", e)
        return True


def inherited_force_semi(run: Dict) -> bool:
    """True when a run that another run started must have EVERY action approved because a run above it is a sensitive role.

    A child's own role does not matter here: the model picks it, and picking a different role must not lift a rule the
    parent is under. The answer is the recorded flag (written once, in the append-only start record, when the child started)
    OR the live parent chain, so pruning or editing the chain can only raise the restriction. A child with no start record at
    all counts as forced. Runs without a parent never touch the database."""
    if not (run or {}).get("parent_id"):
        return False
    try:
        from core import agent_loop
        rec = agent_loop.get_start_record(run.get("id")) if run.get("id") else None
        recorded = rec is None or rec.get("forced") is True
    except Exception as e:
        logger.warning("Sub-agent start record unreadable, forcing approval: %s", e)
        recorded = True
    return recorded or _live_chain_sensitive(run)


def _key_id(raw: str) -> str:
    import hashlib
    return hashlib.sha256(b"ragleap-spawn-key-id|" + raw.encode("utf-8")).hexdigest()[:12]


def _derive(raw: str) -> bytes:
    import hashlib
    return hashlib.sha256(b"ragleap-spawn-record-v1|" + raw.encode("utf-8")).digest()


def _signing_keys():
    """(id of the current key or None, {key id: key} for the current and the previous keys).

    The current key is SPAWN_SIGNING_KEY, else ADDON_ENCRYPTION_KEY. SPAWN_SIGNING_KEY_PREVIOUS may list older keys (comma
    separated) whose records still verify during a rotation. A key id is a short hash and never reveals the key."""
    cur = (os.environ.get("SPAWN_SIGNING_KEY") or os.environ.get("ADDON_ENCRYPTION_KEY") or "").strip()
    prev = [p.strip() for p in (os.environ.get("SPAWN_SIGNING_KEY_PREVIOUS") or "").split(",") if p.strip()]
    keys = {_key_id(r): _derive(r) for r in prev}
    if cur:
        keys[_key_id(cur)] = _derive(cur)
    return (_key_id(cur) if cur else None), keys


def tools_digest(tools) -> Optional[str]:
    """SHA-256 of the sorted tool NAMES a run may use (None for an empty list: a run with no tools is never started)."""
    import hashlib
    names = sorted(str(k) for k in (tools or {}))
    if not names:
        return None
    return hashlib.sha256(json.dumps(names, separators=(",", ":")).encode("utf-8")).hexdigest()


def _spawn_mac(key: bytes, key_id, spawn_id, run_id, parent_id, role, depth, forced, tools_hash) -> str:
    # key_id is in the message on purpose even though a wrong key id already fails today (the key lookup and the key itself
    # differ). It binds a record to the key it names, so a later change that lets two ids share a key cannot let a record be
    # re-labelled. tests/test_subagents.py proves it directly; do not drop it as redundant.
    import hashlib
    import hmac
    # tools_hash (P1-A) binds the exact tool list the child was started with, so a swap between signing and execution is refused.
    msg = "|".join(("spawn3", str(key_id), str(spawn_id), str(run_id), str(parent_id), str(role), str(int(depth)), "1" if forced else "0",
                    str(tools_hash)))
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).hexdigest()


def spawn_record(spawn_id, run_id, parent_id, role, depth, forced, tools_hash) -> Optional[Dict]:
    """The signed start record of a child, written once with its run (the table is append-only). None when it cannot be made."""
    key_id, keys = _signing_keys()
    if key_id is None or not spawn_id or not run_id or not parent_id or not tools_hash:
        return None
    return {"spawn_id": spawn_id, "run_id": run_id, "parent_id": parent_id, "role": role, "depth": int(depth), "forced": bool(forced),
            "tools_hash": tools_hash, "key_id": key_id,
            "sig": _spawn_mac(keys[key_id], key_id, spawn_id, run_id, parent_id, role, depth, forced, tools_hash)}


def verify_spawn_record(run: Dict, tools=None) -> bool:
    """True only when the run has a start record in the append-only table, the record's parent, role and depth are the live
    row's, the tool list the run is about to use (`tools`, else subagents.resolved_tools) has the recorded hash, its key is
    still configured (current or previous) and its signature matches. Anything else is refused."""
    try:
        import hmac
        from core import agent_loop
        run = run or {}
        rid = run.get("id")
        if not rid or not run.get("parent_id"):
            return False
        rec = agent_loop.get_start_record(rid)
        if rec is None:
            return False
        if (str(rec["run_id"]), str(rec["parent_id"]), rec.get("role"), int(rec["depth"])) != \
                (str(rid), str(run["parent_id"]), run.get("role"), int(run.get("depth") or 0)):
            return False
        if tools is None:
            from core import subagents
            tools = subagents.resolved_tools(run)
        have, recorded = tools_digest(tools), rec.get("tools_hash")
        if not have or not recorded or not hmac.compare_digest(str(have), str(recorded)):
            return False                           # a legacy record (no hash), an empty list or a swapped list is refused
        key = _signing_keys()[1].get(rec["key_id"])
        if key is None:
            return False
        # `recorded` and `have` are equal here (the compare above refused otherwise), so signing over either gives the same MAC:
        # a mutation that swaps them is equivalent, not a test gap. The hash check above is what refuses a swapped list.
        want = _spawn_mac(key, rec["key_id"], rec["spawn_id"], rec["run_id"], rec["parent_id"], rec.get("role"), rec["depth"], rec["forced"],
                          recorded)
        return hmac.compare_digest(want, str(rec["sig"]))
    except Exception:
        return False


def authorize_spawn(parent: Dict, target_role: str) -> Dict:
    """THE decision to start a sub-agent (Phase 0, P0-H). execute_delegation (below) starts a child only after this said yes.

    Checks the owner's switch, the depth, the number of children, and tells whether the child will be forced to ask for
    approval for every action (a sensitive role at or above the parent). Every decision is audited. Fails closed."""
    allowed, reason, force, spawn_id = False, "check_failed", True, None
    try:
        import uuid
        from core import subagents
        parent = parent or {}
        if not subagents.enabled():
            reason = "sub_agents_off"
        elif int(parent.get("depth") or 0) >= subagents.MAX_DEPTH:
            reason = "depth_limit"
        elif parent.get("id") and subagents._count_children(parent["id"]) >= subagents.max_children():
            reason = "child_limit"
        elif _signing_keys()[0] is None:
            reason = "no_signing_key"                 # a start record cannot be signed, so no child starts
        else:
            allowed, reason, spawn_id = True, "ok", str(uuid.uuid4())
        force = _role_is_sensitive(parent.get("role")) or inherited_force_semi(parent)
    except Exception as e:
        allowed, reason, force, spawn_id = False, "check_failed", True, None
        logger.warning("Sub-agent authorization failed, refusing: %s", e)
    _audit_event("subagent_authorized" if allowed else "subagent_denied", parent_run=str((parent or {}).get("id")),
                 role=str(target_role), reason=reason, force_approval=force, spawn_id=spawn_id)
    return {"allowed": allowed, "reason": reason, "force_approval": force, "spawn_id": spawn_id}


def execute_delegation(parent: Dict, plan: Dict, service, role: Optional[str]) -> Optional[Dict]:
    """THE ONE way a sub-agent is started (P1-C). The agent loop calls this and nothing else; it asks authorize_spawn and only
    on a yes hands over to subagents.run_delegation (the boundary scan and a tripwire test pin both facts).

    There is no separate owner prompt for the spawn itself: a spawn has no effect on the outside world, and every action a
    child takes goes through execute_or_request on its own (modes, taint, sensitive roles, forced approval). Returns None when
    the parent should go on, or an outcome dict when it must stop."""
    from core import agent_loop, subagents
    if agent_loop._cancelled(parent):
        return agent_loop._finish(parent, None, role, "cancelled", "Stopped: cancelled by the owner.")
    target = str((plan or {}).get("target", "")).strip()
    task = str((plan or {}).get("content", "")).strip()
    if not task or len(task) > subagents.TASK_CAP or target not in subagents._active_roles():
        return agent_loop._finish(parent, service, role, "done", "The proposed sub-agent task was not allowed.")
    auth = authorize_spawn(parent, target)                   # the decision; nothing below runs without its yes
    if not auth["allowed"]:
        if auth["reason"] == "child_limit":
            return agent_loop._finish(parent, service, role, "done", f"Stopped: the limit of {subagents.max_children()} sub-agents was reached.")
        return agent_loop._finish(parent, service, role, "done", "The proposed sub-agent task was not allowed.")
    return subagents.run_delegation(parent, service, role, target, task, auth)


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

    bad_pair = file_pair_error(action_type, channel)
    if bad_pair:
        _audit_event("action_skipped", content=content, reason="action_type and channel do not match", action_type=action_type, channel=channel, role=role)
        return {"status": "error", "result": bad_pair}

    if mode == "off":
        _audit_event("action_skipped", content=content, reason="autonomy off", action_type=action_type, channel=channel, role=role)
        return {"status": "skipped", "result": "autonomy off"}

    if actions and action_type not in actions:
        _audit_event("action_skipped", content=content, reason="action not allowed", action_type=action_type, channel=channel, role=role)
        return {"status": "skipped", "result": f"{action_type} not in allowed actions"}

    if channels and channel not in channels:
        _audit_event("action_skipped", content=content, reason="channel not allowed", action_type=action_type, channel=channel, role=role)
        return {"status": "skipped", "result": f"{channel} not in allowed channels"}

    if channel == "files_write":
        # Bind the write to the real preview first (read-only). A refused or identical preview creates nothing.
        content, why = _bind_file_write(target, content)
        if content is None:
            _audit_event("action_skipped", content=why, reason="write not possible", action_type=action_type, channel=channel, role=role)
            return {"status": "skipped", "result": why}

    if channel == "files_delete":
        # Bind the delete to the real file (read-only stat). A missing or refused file creates nothing.
        content, why = _bind_file_delete(target, content)
        if content is None:
            _audit_event("action_skipped", content=why, reason="delete not possible", action_type=action_type, channel=channel, role=role)
            return {"status": "skipped", "result": why}

    if mode == "full" and (force_semi or action_type in _FORCED_ACTION_TYPES or channel in _FORCED_CHANNELS):
        # caller (e.g. a run that has read untrusted web/MCP content) requires approval for this call
        logger.info(f"force_semi: {action_type} forced full->semi")
        _audit_event("action_forced_approval", content=content, reason="untrusted input, request_access, write_file or delete_file", action_type=action_type, channel=channel, role=role)
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
        subject = subject or ""
        stored, ahash = None, None
        pdigest = policy_digest()                   # the settings this approval is given under
        conn = get_connection()
        try:
            cur = conn.cursor()
            for _ in range(6):                      # a clash on the short id draws a new id; the prompt is only sent for a stored action
                candidate = str(uuid.uuid4())[:8].upper()
                h, sig = approval_binding(candidate, action_type, channel, target, content, subject, role, policy=pdigest)
                cur.execute(
                    "INSERT INTO autonomy_pending (action_id, action_type, channel, target, content, subject, role, action_hash, action_sig, "
                    "policy_hash) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (action_id) DO NOTHING RETURNING action_id",
                    (candidate, action_type, channel, target, content, subject, role, h, sig, pdigest),
                )
                if cur.fetchone():
                    stored, ahash = candidate, h
                    break
            conn.commit()
            cur.close()
        except Exception as e:
            stored, ahash = None, None
            logger.warning(f"failed to store pending action: {e}")
        finally:
            conn.close()
        if not stored:
            _audit_event("action_not_stored", action_type=action_type, channel=channel, target=target, role=role)
            return {"status": "error", "result": "could not store the action for approval; nothing was sent"}
        action_id = stored

        _audit_event("action_proposed", content=content, action_id=action_id, action_type=action_type, channel=channel, target=target, role=role,
                     action_hash=ahash)
        sent = request_approval(action_type, channel, target, content, action_id, role=role, ref=ahash)
        return {"status": "pending_approval", "action_id": action_id, "approval_sent": sent}

    return {"status": "skipped", "result": "unknown mode"}


def _notify_agent_loop(action_id: str, approved: bool, result: str, action_type: str) -> None:
    """Let a waiting agent-loop run (if this pending action belongs to one) resume or end."""
    try:
        from core import agent_loop
        agent_loop.on_resolved(action_id, approved, result, action_type)
    except Exception as e:
        logger.warning(f"agent loop notification failed (non-fatal): {e}")


def _approval_expiry_hours() -> int:
    """How long a pending action may wait for an answer. APPROVAL_EXPIRY_HOURS, default 168 (7 days); 0 turns it off."""
    try:
        h = int(os.environ.get("APPROVAL_EXPIRY_HOURS", "168"))
    except ValueError:
        h = 168
    return h if h > 0 else 876000


def process_approval_response(message: str, decided_by: Optional[Dict] = None, expected_hash: Optional[str] = None) -> Optional[str]:
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
        # Claim the pending row atomically: only the caller whose DELETE removes the row may
        # run the effect. A second, simultaneous answer (dashboard, API or chat) finds no row.
        # expected_hash (the Ref the owner saw, 12 or more hex characters) must match the stored hash, else nothing is consumed.
        eh = (expected_hash or "").strip().lower() or None
        cur.execute(
            "DELETE FROM autonomy_pending WHERE action_id = %s AND (%s::text IS NULL OR left(coalesce(action_hash, ''), length(%s::text)) = %s::text) "
            "RETURNING action_type, channel, target, content, subject, role, "
            "(created_at < now() - make_interval(hours => %s)), action_hash, action_sig, policy_hash",
            (action_id, eh, eh, eh, _approval_expiry_hours()),
        )
        row = cur.fetchone()
        conn.commit()
        if not row:
            cur.close()
            if eh:
                return f"Action {action_id} was not run: not found, already processed, or the reference does not match."
            return f"Action {action_id} not found or already processed."

        action_type, channel, target, content, subject, role, expired, ahash, asig, apol = row
        cur.close()
    except Exception as e:
        logger.error(f"process_approval_response lookup error: {e}", exc_info=True)
        return "Error processing approval; see server logs."
    finally:
        conn.close()

    if not approved:
        log_autonomous_action(action_type, channel, target, content, "REJECTED by owner", approved=False, role=role,
                              decided_by=decided_by, action_id=action_id)
        employee_learning.learn_from_owner_approval(
            action_type, _log_content(channel, content), "REJECTED by owner", approved=False
        )
        _notify_agent_loop(action_id, False, "", action_type)
        return f"Action {action_id} rejected and cancelled."

    if expired:
        # An old request is not a current decision: the situation may have changed since it was asked.
        log_autonomous_action(action_type, channel, target, content, "EXPIRED: not run", approved=False, role=role,
                              decided_by=decided_by, action_id=action_id)
        _notify_agent_loop(action_id, False, "", action_type)
        return (f"Action {action_id} expired (it waited longer than {_approval_expiry_hours()} hours) and was not run. "
                "Ask for it again if you still want it.")

    ran, result = _execute_approved(action_id, {"action_type": action_type, "channel": channel, "target": target, "content": content,
                                                "subject": subject, "role": role}, ahash, asig, apol)
    if not ran:
        text = _REFUSAL_TEXT.get(result, "its integrity check failed")
        log_autonomous_action(action_type, channel, target, content, f"REFUSED: {text} ({result})",
                              approved=False, role=role, decided_by=decided_by, action_id=action_id)
        _notify_agent_loop(action_id, False, "", action_type)
        return f"Action {action_id} was NOT run: {text} ({result}). Ask for it again."
    log_autonomous_action(action_type, channel, target, content, result, approved=True, role=role,
                          decided_by=decided_by, action_id=action_id)
    employee_learning.learn_from_owner_approval(
        action_type, _log_content(channel, content), result, approved=True
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


def _linked_approver(channel: str, sender) -> Optional[Dict]:
    """A person whose chat is linked (core/users.py) and who may approve actions (operator or higher), else None.
    Fails closed: any error means nobody."""
    try:
        from core import permissions, users
        person = users.resolve_channel(channel, sender)
        if person and permissions.allows(person["role"], "operator"):
            return {"name": person["name"], "role": person["role"], "kind": "channel", "user_id": person["user_id"]}
    except Exception as e:
        logger.warning(f"Linked-chat check failed (treating sender as unknown): {e}")
    return None


def process_approval_from(channel: str, sender, message: str, private: bool = True, actor=None) -> Optional[str]:
    """Like process_approval_response, but only honours the reply when it comes from the
    configured owner or a person whose chat is linked and who may approve. Anyone else gets None (the message
    falls through to normal chat), so a
    stranger cannot approve or reject actions, or learn whether an action id exists."""
    # An approval counts only from a one-to-one conversation. In a group chat or a server channel the id
    # that arrives is the group's, so anyone in it would count as the owner (F-CORE-006). When the caller
    # also knows the person who wrote the message, that person must be the conversation's owner id.
    if not private or (actor is not None and _norm_sender(channel, actor) != _norm_sender(channel, sender)):
        if re.match(r"^\s*(YES|NO)\s+[A-Za-z0-9]{8}\s*$", message or "", re.IGNORECASE):
            logger.warning(f"Approval-style message on {channel} ignored: approvals are accepted only in a private chat")
        return None
    if not is_owner_sender(channel, sender):
        person = _linked_approver(channel, sender)
        if person is not None:
            return process_approval_response(message, decided_by=person)
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
