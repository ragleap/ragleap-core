"""The declaration registry (Phase 0, draft): declarations are immutable once registered.

- A declaration is registered once, at startup, as a deep frozen snapshot with a SHA-256 hash of its canonical JSON.
- The same id with a different hash is REJECTED (and audited). The same hash is a no-op.
- There is no update path. A change needs a new version and a restart. After seal() no registration is accepted.
"""
import copy
import types
from typing import Any, Callable, Dict, List, Optional

from core.connector_host.policy import BUILTIN_EFFECT, EFFECT_RANK, canonical_hash
from core.connector_host.schema import SCHEMA_V1, check_declaration

SUPPORTED_SCHEMA = "ragleap.capability/1"


class RegistryError(Exception):
    def __init__(self, code: str, message: str = "", details: Optional[List[str]] = None):
        super().__init__(f"{code}: {message}" if message else code)
        self.code, self.message, self.details = code, message, details or []


def _freeze(obj):
    if isinstance(obj, dict):
        return types.MappingProxyType({k: _freeze(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return tuple(_freeze(v) for v in obj)
    return obj


def _thaw(obj):
    if isinstance(obj, types.MappingProxyType):
        return {k: _thaw(v) for k, v in obj.items()}
    if isinstance(obj, tuple):
        return [_thaw(v) for v in obj]
    return obj


class RegisteredDeclaration:
    """A frozen snapshot. `data` is read-only (nested mappings and tuples); to_dict() returns an independent plain copy."""
    __slots__ = ("id", "version", "hash", "data")

    def __init__(self, decl: Dict[str, Any], digest: str):
        object.__setattr__(self, "id", decl["id"])
        object.__setattr__(self, "version", decl["version"])
        object.__setattr__(self, "hash", digest)
        object.__setattr__(self, "data", _freeze(copy.deepcopy(decl)))

    def __setattr__(self, name, value):
        raise AttributeError("a registered declaration is immutable")

    def to_dict(self) -> Dict[str, Any]:
        return _thaw(self.data)


class DeclarationRegistry:
    def __init__(self, audit: Optional[Callable[[Dict[str, Any]], None]] = None):
        self._items: Dict[str, RegisteredDeclaration] = {}
        self._sealed = False
        self._audit = audit or (lambda event: None)

    def _reject(self, code: str, message: str, decl: Any, details: Optional[List[str]] = None):
        did = decl.get("id") if isinstance(decl, dict) else None
        try:
            dh = canonical_hash(decl)
        except (TypeError, ValueError):
            dh = ""
        self._audit({"event": "declaration_rejected", "reason": code, "declaration_id": did, "declaration_hash": dh})
        raise RegistryError(code, message, details)

    def register(self, declaration: Dict[str, Any]) -> RegisteredDeclaration:
        decl = copy.deepcopy(declaration)
        if self._sealed:
            self._reject("registry_sealed", "declarations can only be registered at startup", decl)
        schema_str = decl.get("schema") if isinstance(decl, dict) else None
        if isinstance(schema_str, str) and schema_str.startswith("ragleap.capability/") and schema_str != SUPPORTED_SCHEMA:
            self._reject("unsupported_schema_version", f"this core reads {SUPPORTED_SCHEMA} only", decl)
        errors = [m for lvl, code, m in check_declaration(decl, SCHEMA_V1) if lvl == "error"]
        if errors:
            self._reject("invalid_declaration", "the declaration does not conform", decl, errors)
        for op in decl["operations"]:
            core = BUILTIN_EFFECT.get(op["name"])
            if core and EFFECT_RANK[op["effect"]] < EFFECT_RANK[core]:
                self._reject("effect_weaker_than_core", f"operation {op['name']} claims {op['effect']} but the core classifies it as {core}", decl)
        digest = canonical_hash(decl)
        existing = self._items.get(decl["id"])
        if existing is not None:
            if existing.hash == digest:
                return existing
            self._audit({"event": "declaration_rejected", "reason": "declaration_conflict", "declaration_id": decl["id"],
                         "declaration_hash": digest, "registered_hash": existing.hash})
            raise RegistryError("declaration_conflict", f"{decl['id']} is already registered with a different declaration")
        reg = RegisteredDeclaration(decl, digest)
        self._items[decl["id"]] = reg
        self._audit({"event": "declaration_registered", "declaration_id": reg.id, "declaration_version": reg.version, "declaration_hash": reg.hash})
        return reg

    def seal(self) -> None:
        """Called once startup has finished: later registration is refused."""
        self._sealed = True

    def get(self, declaration_id: str) -> RegisteredDeclaration:
        return self._items[declaration_id]

    def ids(self) -> List[str]:
        return sorted(self._items)
