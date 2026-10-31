"""Single sign-on for people: who may sign in, one-time sign-in attempts, and sessions.

core/oidc.py checks the provider's answer; this module decides what it means for RagLeap. Only people the owner created can
sign in: the owner registers an email for a person, and the first sign-in binds the provider's subject id to that person, so
a later email change at the provider cannot take over the account. A session is a random token (stored only as a hash) that
works in the same x-api-key header as a personal key, with the same roles and audit. Sessions end after SSO_SESSION_HOURS
(default 8) or SSO_IDLE_MINUTES without use (default 30), when they are revoked, or the moment the person is disabled."""
import hashlib
import logging
import os
import re
import secrets

from core import audit, oidc
from core.users import UserError, _connect, _is_unique_violation, _uuid

logger = logging.getLogger(__name__)

SESSION_PREFIX = "rls_"
PENDING_MINUTES = 10
MAX_PENDING = 1000
NOT_REGISTERED = "You are not registered for this system. Ask the owner to add your email address."
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,189}$")
_warned = set()


class SsoDenied(oidc.OidcError):
    """A sign-in that is valid at the provider but not allowed here. `status` is what is written to the audit record."""

    def __init__(self, public, detail="", status=403, email="", user_id=None):
        super().__init__(public, detail)
        self.status, self.email, self.user_id = status, email, user_id


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _int_env(name, default, low, high):
    try:
        return min(high, max(low, int(os.environ.get(name, "") or default)))
    except ValueError:
        return default


def session_hours():
    return _int_env("SSO_SESSION_HOURS", 8, 1, 168)


def idle_minutes():
    return _int_env("SSO_IDLE_MINUTES", 30, 5, 1440)


def _config():
    try:
        return oidc.load_config()
    except oidc.OidcError as e:
        if "config" not in _warned:
            _warned.add("config")
            logger.warning("single sign-on is configured wrongly: %s", e.detail or e.public)
        return None


def enabled() -> bool:
    return _config() is not None


# ---- the owner registers who may sign in ----

def _clean_email(email):
    value = email.strip().lower() if isinstance(email, str) else ""
    if not _EMAIL.match(value):
        raise UserError(400, "give a valid email address")
    return value


def register_identity(user_id, email) -> dict:
    cfg = _config()
    if cfg is None:
        raise UserError(409, "Single sign-on is not set up (the OIDC_* settings).")
    user_id, email = _uuid(user_id), _clean_email(email)
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT status FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        if row is None:
            raise UserError(404, "not found")
        if row[0] != "active":
            raise UserError(409, "that person is disabled")
        try:
            cur.execute("INSERT INTO user_identities (user_id, issuer, email) VALUES (%s, %s, %s) RETURNING id",
                        (user_id, cfg.issuer, email))
        except Exception as e:
            conn.rollback()
            if _is_unique_violation(e):
                raise UserError(409, "that email is already registered")
            raise
        identity_id = cur.fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    return {"id": str(identity_id), "email": email, "issuer": cfg.issuer, "bound": False}


def list_identities(user_id) -> list:
    user_id = _uuid(user_id)
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, issuer, email, subject IS NOT NULL, last_login_at FROM user_identities WHERE user_id = %s "
                    "ORDER BY created_at", (user_id,))
        return [{"id": str(r[0]), "issuer": r[1], "email": r[2], "bound": bool(r[3]),
                 "last_login_at": r[4].isoformat() if r[4] else None} for r in cur.fetchall()]
    finally:
        conn.close()


def remove_identity(user_id, identity_id) -> dict:
    user_id, identity_id = _uuid(user_id), _uuid(identity_id)
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM user_identities WHERE id = %s AND user_id = %s RETURNING id", (identity_id, user_id))
        row = cur.fetchone()
        conn.commit()
    finally:
        conn.close()
    if row is None:
        raise UserError(404, "not found")
    return {"removed": str(row[0])}


def revoke_sessions(user_id) -> dict:
    user_id = _uuid(user_id)
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE user_sessions SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL", (user_id,))
        count = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return {"revoked": count}


# ---- signing in ----

def start_login() -> str:
    """Remembers one pending sign-in (state, nonce, PKCE verifier) and returns the provider's sign-in address."""
    cfg = _config()
    if cfg is None:
        raise oidc.OidcError("Single sign-on is not set up.")
    disc = oidc.discover(cfg)
    state, nonce, verifier = oidc.new_state(), oidc.new_state(), oidc.new_verifier()
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM sso_logins WHERE expires_at < now()")
        cur.execute("SELECT count(*) FROM sso_logins")
        if cur.fetchone()[0] >= MAX_PENDING:
            conn.commit()
            raise oidc.OidcError("Too many sign-ins are in progress. Try again in a few minutes.", "pending sign-in limit reached")
        cur.execute("INSERT INTO sso_logins (state_hash, nonce, verifier, expires_at) VALUES (%s, %s, %s, now() + make_interval(mins => %s))",
                    (_hash(state), nonce, verifier, PENDING_MINUTES))
        conn.commit()
    finally:
        conn.close()
    return oidc.authorization_url(cfg, disc, state, nonce, oidc.pkce_challenge(verifier))


def _match_person(who):
    """The registered, active person for this provider identity. Binds the subject on the first sign-in."""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT i.id, i.user_id, u.name, u.role, u.status FROM user_identities i JOIN users u ON u.id = i.user_id "
                    "WHERE i.issuer = %s AND i.subject = %s", (who.issuer, who.subject))
        row = cur.fetchone()
        if row is None:
            cur.execute("SELECT i.id, i.user_id, u.name, u.role, u.status, i.subject FROM user_identities i "
                        "JOIN users u ON u.id = i.user_id WHERE i.issuer = %s AND i.email = %s", (who.issuer, who.email))
            found = cur.fetchone()
            if found is None:
                raise SsoDenied(NOT_REGISTERED, "no person registered for this email", 403, who.email)
            if found[5] is not None:
                raise SsoDenied(NOT_REGISTERED, "this email is bound to a different provider subject", 403, who.email, str(found[1]))
            cur.execute("UPDATE user_identities SET subject = %s WHERE id = %s AND subject IS NULL", (who.subject, found[0]))
            if cur.rowcount != 1:
                conn.rollback()
                raise SsoDenied(NOT_REGISTERED, "the identity was bound by another sign-in at the same time", 403, who.email, str(found[1]))
            row = found[:5]
        if row[4] != "active":
            raise SsoDenied(NOT_REGISTERED, "the person is disabled", 403, who.email, str(row[1]))
        cur.execute("UPDATE user_identities SET last_login_at = now() WHERE id = %s", (row[0],))
        conn.commit()
        return {"user_id": str(row[1]), "name": row[2], "role": row[3]}
    finally:
        conn.close()


def _new_session(user_id) -> dict:
    token = SESSION_PREFIX + secrets.token_urlsafe(32)
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO user_sessions (user_id, token_hash, expires_at) VALUES (%s, %s, now() + make_interval(hours => %s)) "
                    "RETURNING expires_at", (user_id, _hash(token), session_hours()))
        expires = cur.fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    return {"token": token, "expires_at": expires.isoformat()}


def finish_login(state, code, error="") -> dict:
    """Completes a sign-in. Raises OidcError (or SsoDenied) with a message that is safe to show."""
    cfg = _config()
    if cfg is None:
        raise oidc.OidcError("Single sign-on is not set up.")
    if not isinstance(state, str) or not 8 <= len(state) <= 200:
        raise oidc.OidcError("This sign-in link is not valid. Please start again.", "state missing or malformed")
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM sso_logins WHERE state_hash = %s AND expires_at > now() RETURNING nonce, verifier", (_hash(state),))
        pending = cur.fetchone()                  # one use only: a replayed or expired link finds nothing
        conn.commit()
    finally:
        conn.close()
    if pending is None:
        raise oidc.OidcError("This sign-in has expired or was already used. Please start again.", "state unknown, expired or replayed")
    if error:
        raise oidc.OidcError("The sign-in was cancelled or refused by the provider.", "provider error: " + re.sub(r"[^A-Za-z_ ]", "", str(error))[:40])
    disc = oidc.discover(cfg)
    who = oidc.validate_id_token(cfg, disc, oidc.exchange_code(cfg, disc, code, pending[1]), pending[0])
    try:
        person = _match_person(who)
    except SsoDenied:
        raise
    session = _new_session(person["user_id"])
    return dict(session, **person, email=who.email)


def audit_success(result):
    audit.record({"kind": "user", "name": result["name"], "role": result["role"], "user_id": result["user_id"]}, "POST", "/auth/callback", 200)


def audit_failure(exc):
    name = getattr(exc, "email", "") or "unknown sign-in"
    audit.record({"kind": "sso", "name": name, "role": "none", "user_id": getattr(exc, "user_id", None)},
                 "POST", "/auth/callback", getattr(exc, "status", 401))


# ---- sessions ----

def resolve_session(token):
    """The person behind a live session as an actor dict, or None. Never raises. Not cached: a revoked or disabled
    session stops working on the next request."""
    if not isinstance(token, str) or not token.startswith(SESSION_PREFIX) or len(token) > 128:
        return None
    try:
        conn = _connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT s.id, u.id, u.name, u.role FROM user_sessions s JOIN users u ON u.id = s.user_id "
                        "WHERE s.token_hash = %s AND s.revoked_at IS NULL AND s.expires_at > now() "
                        "AND s.last_used_at > now() - make_interval(mins => %s) AND u.status = 'active'",
                        (_hash(token), idle_minutes()))
            row = cur.fetchone()
            if row is None:
                return None
            cur.execute("UPDATE user_sessions SET last_used_at = now() WHERE id = %s AND last_used_at < now() - interval '1 minute'", (row[0],))
            conn.commit()
            return {"kind": "user", "user_id": str(row[1]), "name": row[2], "role": row[3], "key_id": None, "via": "sso", "session_id": str(row[0])}
        finally:
            conn.close()
    except Exception as e:
        if "session" not in _warned:
            _warned.add("session")
            logger.warning("session lookup failed (%s); is db/schema.sql applied?", type(e).__name__)
        return None


def logout(session_id) -> bool:
    """Ends one session by its id, so the route never has to read the token again."""
    try:
        session_id = _uuid(session_id)
    except UserError:
        return False
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE user_sessions SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL RETURNING id", (session_id,))
        row = cur.fetchone()
        conn.commit()
    finally:
        conn.close()
    return row is not None
