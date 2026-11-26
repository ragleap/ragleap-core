"""ragleap-security: small, auditable security primitives for the ragleap libraries.

The core package is standard library only. Encryption lives in ragleap_security.crypto and needs the
optional extra: pip install ragleap-security[crypto]. It is not imported here, so the core stays
dependency-free. ragleap_security.net (SSRF-safe outbound HTTP, standard library only) is imported on its own:
from ragleap_security.net import SafeFetcher, FetchPolicy. ragleap_security.canary (canary tokens that detect and trace leaks, never prevent them; standard library only) is also imported on its own. Not independently audited. See docs/threat-model.md for what is and is not
defended."""
from ragleap_security.audit import GENESIS, AuditError, AuditLog, VerifyResult, verify
from ragleap_security.integrity import IntegrityError, IntegrityResult, Manifest, build_manifest, file_digest, verify_manifest
from ragleap_security.keys import CryptoError, Key, KeyRing
from ragleap_security.limits import Deadline, LimitError, bounded_read, check_json_shape, check_size
from ragleap_security.ratelimit import Decision, RateLimiter, RateLimitError, SQLiteRateLimiter
from ragleap_security.scope import Scope, ScopeError, require_scope
from ragleap_security.secrets import SecretError, SecretStr, from_env, redact

__version__ = "0.4.0"

__all__ = [
    "AuditError", "AuditLog", "CryptoError", "Deadline", "Decision", "GENESIS", "IntegrityError", "IntegrityResult",
    "Key", "KeyRing", "LimitError", "Manifest", "RateLimitError", "RateLimiter", "SQLiteRateLimiter", "Scope",
    "ScopeError", "SecretError", "SecretStr", "VerifyResult", "__version__", "bounded_read", "build_manifest",
    "check_json_shape", "check_size", "file_digest", "from_env", "redact", "require_scope", "verify",
    "verify_manifest",
]
