"""
People and their personal keys.

Each person gets a role (core/permissions.py) and one or more personal keys. A key is a long random string that is
shown ONCE when it is made; only its sha256 hash is stored (a random 256-bit key needs no slow password hash). The
old shared RAGLEAP_API_KEY keeps working and counts as the owner; see require_api_key in core/api.py.

resolve() is called on requests that carry a personal key. It never raises: any problem (a missing table, a database
error) means "unknown key", which is refused. A resolved key is cached in this process for 30 seconds, so revoking a
key or disabling a person takes effect at once in this process and within 30 seconds in any other.
"""
import hashlib
import logging
import re
import secrets
import time
import uuid

from core.permissions import ROLES

logger = logging.getLogger(__name__)

KEY_PREFIX = "rlk_"
CACHE_SECONDS = 30
LAST_USED_EVERY_MINUTES = 5
_cache = {}
_warned = set()


class UserError(Exception):
    """A refused request with the HTTP status to answer it with."""
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _connect():
    from core import document_store
    return document_store._connect()


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def new_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def _clear_cache() -> None:
    _cache.clear()


def _uuid(value) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        raise UserError(404, "not found")


def _clean_name(name) -> str:
    if not isinstance(name, str):
        raise UserError(400, "name must be text")
    name = name.strip()
    if not 1 <= len(name) <= 80 or re.search(r"[\x00-\x1f\x7f]", name):
        raise UserError(400, "name must be 1 to 80 characters, with no control characters")
    return name


def _clean_role(role) -> str:
    if role not in ROLES:
        raise UserError(400, "role must be one of: " + ", ".join(ROLES))
    return role


def _is_unique_violation(e) -> bool:
    return e.__class__.__name__ == "UniqueViolation"


def _user_dict(row) -> dict:
    return {"id": str(row[0]), "name": row[1], "role": row[2], "status": row[3],
            "created_at": row[4].isoformat() if row[4] else None}


# ---------------- sign-in ----------------

def _lookup(key_hash: str):
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT u.id, u.name, u.role, k.id FROM user_keys k JOIN users u ON u.id = k.user_id "
                    "WHERE k.key_hash = %s AND k.revoked_at IS NULL AND u.status = 'active'", (key_hash,))
        row = cur.fetchone()
        if row is None:
            return None
        cur.execute("UPDATE user_keys SET last_used_at = now() WHERE id = %s AND "
                    "(last_used_at IS NULL OR last_used_at < now() - make_interval(mins => %s))",
                    (row[3], LAST_USED_EVERY_MINUTES))
        conn.commit()
        return {"kind": "user", "user_id": str(row[0]), "name": row[1], "role": row[2], "key_id": str(row[3])}
    finally:
        conn.close()


def resolve(key):
    """The person behind a personal key as {"kind", "user_id", "name", "role", "key_id"}, or None. Never raises."""
    if not isinstance(key, str) or not key.startswith(KEY_PREFIX) or len(key) > 128:
        return None
    key_hash = hash_key(key)
    hit = _cache.get(key_hash)
    if hit and hit[0] > time.monotonic():
        return dict(hit[1])
    try:
        actor = _lookup(key_hash)
    except Exception as e:
        if "lookup" not in _warned:
            _warned.add("lookup")
            logger.warning("personal key lookup failed (%s); is db/schema.sql applied?", type(e).__name__)
        return None
    if actor is not None:
        if len(_cache) > 1000:
            _cache.clear()
        _cache[key_hash] = (time.monotonic() + CACHE_SECONDS, actor)
    return dict(actor) if actor else None


# ---------------- managing people (owner only; see permissions.RULES) ----------------

def create_user(name, role) -> dict:
    name, role = _clean_name(name), _clean_role(role)
    conn = _connect()
    try:
        cur = conn.cursor()
        try:
            cur.execute("INSERT INTO users (name, role) VALUES (%s, %s) RETURNING id, name, role, status, created_at",
                        (name, role))
        except Exception as e:
            conn.rollback()
            if _is_unique_violation(e):
                raise UserError(409, "a person with that name already exists")
            raise
        row = cur.fetchone()
        conn.commit()
    finally:
        conn.close()
    logger.info("user created: %s (%s)", name, role)
    return _user_dict(row)


def list_users() -> list:
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT u.id, u.name, u.role, u.status, u.created_at, "
                    "COUNT(k.id) FILTER (WHERE k.revoked_at IS NULL), MAX(k.last_used_at) "
                    "FROM users u LEFT JOIN user_keys k ON k.user_id = u.id "
                    "GROUP BY u.id ORDER BY u.created_at, u.name")
        rows = cur.fetchall()
    finally:
        conn.close()
    return [dict(_user_dict(r), active_keys=int(r[5]), last_used_at=r[6].isoformat() if r[6] else None)
            for r in rows]


def update_user(user_id, role=None, status=None) -> dict:
    user_id = _uuid(user_id)
    if role is None and status is None:
        raise UserError(400, "nothing to change")
    if role is not None:
        _clean_role(role)
    if status is not None and status not in ("active", "disabled"):
        raise UserError(400, "status must be active or disabled")
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE users SET role = COALESCE(%s, role), status = COALESCE(%s, status) WHERE id = %s "
                    "RETURNING id, name, role, status, created_at", (role, status, user_id))
        row = cur.fetchone()
        conn.commit()
    finally:
        conn.close()
    _clear_cache()
    if row is None:
        raise UserError(404, "not found")
    logger.info("user changed: %s (%s, %s)", row[1], row[2], row[3])
    return _user_dict(row)


def create_key(user_id, label="") -> dict:
    """Make a personal key for an active person. The key itself is in the answer once and is never stored."""
    user_id = _uuid(user_id)
    if not isinstance(label, str) or len(label) > 80 or re.search(r"[\x00-\x1f\x7f]", label):
        raise UserError(400, "label must be at most 80 characters, with no control characters")
    key = new_key()
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT status FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        if row is None:
            raise UserError(404, "not found")
        if row[0] != "active":
            raise UserError(409, "that person is disabled")
        cur.execute("INSERT INTO user_keys (user_id, key_hash, key_prefix, label) VALUES (%s, %s, %s, %s) "
                    "RETURNING id, created_at", (user_id, hash_key(key), key[:8], label.strip()))
        key_id, created = cur.fetchone()
        conn.commit()
    finally:
        conn.close()
    logger.info("personal key created for user %s (%s...)", user_id, key[:8])
    return {"key_id": str(key_id), "key": key, "key_prefix": key[:8], "label": label.strip(),
            "created_at": created.isoformat(), "note": "Copy this key now. It is shown only once."}


def revoke_key(user_id, key_id) -> dict:
    user_id, key_id = _uuid(user_id), _uuid(key_id)
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE user_keys SET revoked_at = now() WHERE id = %s AND user_id = %s AND revoked_at IS NULL "
                    "RETURNING id", (key_id, user_id))
        row = cur.fetchone()
        conn.commit()
    finally:
        conn.close()
    _clear_cache()
    if row is None:
        raise UserError(404, "not found, or already revoked")
    logger.info("personal key revoked for user %s", user_id)
    return {"revoked": str(row[0])}
