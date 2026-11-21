"""Capability declaration validation shared by the core and the local conformance checker (Phase 0, draft).

Single source for the checks: scripts/ragleap_conformance.py (offline checker for contributors) and the declaration
registry of the core both use this module. SCHEMA_V1 is an embedded copy of docs/capability-schema-v1.schema.json (a
test keeps the two identical), so the core never depends on the docs folder at run time.
"""
import ipaddress
import json
import re

SCHEMA_V1_JSON = r'''{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "urn:ragleap:capability:1",
  "title": "RagLeap capability declaration, version 1 (draft)",
  "description": "What an integration says it can do. The RagLeap core reads it and enforces its own rules; a declaration can only narrow what is allowed, never widen it. Fields under descriptive are not enforced.",
  "type": "object",
  "additionalProperties": false,
  "required": ["schema", "id", "version", "kind", "operations"],
  "properties": {
    "schema": {"const": "ragleap.capability/1"},
    "id": {"type": "string", "maxLength": 64, "pattern": "^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)+$"},
    "version": {"type": "string", "pattern": "^(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)$"},
    "kind": {"enum": ["action", "datasource", "mcp"]},
    "operations": {"type": "array", "minItems": 1, "maxItems": 64, "items": {"$ref": "#/$defs/operation"}},
    "scope": {"$ref": "#/$defs/scope"},
    "network": {"$ref": "#/$defs/network"},
    "secrets": {"type": "array", "maxItems": 16, "uniqueItems": true, "items": {"type": "string", "pattern": "^[A-Z][A-Z0-9_]*$", "maxLength": 64}},
    "budgets": {"$ref": "#/$defs/budgets"},
    "descriptive": {"$ref": "#/$defs/descriptive"}
  },
  "$defs": {
    "operation": {
      "type": "object",
      "additionalProperties": false,
      "required": ["name", "effect"],
      "properties": {
        "name": {"type": "string", "maxLength": 48, "pattern": "^[a-z][a-z0-9_]*$"},
        "effect": {"enum": ["read", "write", "external_send", "execute"]},
        "approval": {"enum": ["always", "per_mode"]},
        "taint": {"enum": ["none", "tainting", "outbound", "both"]},
        "args": {"type": "string", "maxLength": 200}
      }
    },
    "scope": {
      "type": "object",
      "additionalProperties": false,
      "required": ["level"],
      "properties": {
        "level": {"enum": ["organization", "user"]},
        "user_filter_field": {"type": "string", "maxLength": 64, "pattern": "^[A-Za-z_][A-Za-z0-9_]*$"}
      }
    },
    "network": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "hosts": {"type": "array", "maxItems": 32, "uniqueItems": true, "items": {"type": "string", "maxLength": 253, "pattern": "^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$"}},
        "https_only": {"const": true},
        "tcp": {"type": "array", "maxItems": 8, "items": {"$ref": "#/$defs/tcp"}}
      }
    },
    "tcp": {
      "type": "object",
      "additionalProperties": false,
      "required": ["host", "port"],
      "properties": {
        "host": {"type": "string", "maxLength": 253, "pattern": "^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$"},
        "port": {"type": "integer", "minimum": 1, "maximum": 65535}
      }
    },
    "budgets": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "max_calls_per_run": {"type": "integer", "minimum": 1, "maximum": 1000},
        "max_calls_per_day": {"type": "integer", "minimum": 1, "maximum": 100000},
        "max_bytes": {"type": "integer", "minimum": 1, "maximum": 104857600},
        "timeout_s": {"type": "integer", "minimum": 1, "maximum": 300}
      }
    },
    "descriptive": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "title": {"type": "string", "maxLength": 100},
        "notes": {"type": "string", "maxLength": 500},
        "data_classes": {"type": "array", "maxItems": 16, "uniqueItems": true, "items": {"type": "string", "maxLength": 32, "pattern": "^[a-z][a-z_]*$"}}
      }
    }
  }
}'''
SCHEMA_V1 = json.loads(SCHEMA_V1_JSON)

# Secrets owned by the core: a connector may not declare them.
PROTECTED_SECRETS = {"RAGLEAP_API_KEY", "SANDBOX_TOKEN", "NEO4J_PASSWORD", "DATABASE_URL", "SHARED_KEY_DISABLED",
                     "POSTGRES_PASSWORD", "ADDON_ENCRYPTION_KEY"}
# Names of internal-only hosts that must not appear in a declaration (the owner allow list is the place for those).
INTERNAL_SUFFIXES = (".local", ".internal", ".lan", ".localdomain", ".home.arpa")
KNOWN_DATA_CLASSES = {"personal", "financial", "health", "credentials", "confidential", "public"}


# ---------------------------------------------------------------- small JSON Schema subset validator

def _type_ok(value, t):
    if t == "object":
        return isinstance(value, dict)
    if t == "array":
        return isinstance(value, list)
    if t == "string":
        return isinstance(value, str)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "boolean":
        return isinstance(value, bool)
    return True


def validate(instance, schema, root=None, path="$"):
    """Return a list of (path, message) for the subset of JSON Schema used by the capability schema."""
    root = root if root is not None else schema
    errors = []
    if "$ref" in schema:
        node = root
        for part in schema["$ref"].lstrip("#/").split("/"):
            node = node[part]
        return validate(instance, node, root, path)
    if "const" in schema and instance != schema["const"]:
        errors.append((path, f"must be {schema['const']!r}"))
        return errors
    if "enum" in schema and instance not in schema["enum"]:
        errors.append((path, f"must be one of {schema['enum']}"))
        return errors
    t = schema.get("type")
    if t and not _type_ok(instance, t):
        errors.append((path, f"must be of type {t}"))
        return errors
    if isinstance(instance, str):
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append((path, f"is longer than {schema['maxLength']} characters"))
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            errors.append((path, f"does not match the required pattern {schema['pattern']}"))
    if isinstance(instance, int) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append((path, f"must be at least {schema['minimum']}"))
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append((path, f"must be at most {schema['maximum']}"))
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append((path, f"needs at least {schema['minItems']} item(s)"))
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append((path, f"has more than {schema['maxItems']} items"))
        if schema.get("uniqueItems"):
            seen = []
            for it in instance:
                if it in seen:
                    errors.append((path, f"has a duplicate item {it!r}"))
                seen.append(it)
        if "items" in schema:
            for i, it in enumerate(instance):
                errors += validate(it, schema["items"], root, f"{path}[{i}]")
    if isinstance(instance, dict):
        for r in schema.get("required", []):
            if r not in instance:
                errors.append((path, f"is missing the required field {r!r}"))
        props = schema.get("properties", {})
        for k, v in instance.items():
            if k in props:
                errors += validate(v, props[k], root, f"{path}.{k}")
            elif schema.get("additionalProperties") is False:
                errors.append((path, f"has an unknown field {k!r}"))
    return errors


# ---------------------------------------------------------------- semantic rules

def _is_internal_host(host: str) -> bool:
    h = host.lower().rstrip(".")
    if h == "localhost" or h.endswith(INTERNAL_SUFFIXES):
        return True
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return False


def semantic_findings(decl: dict):
    """Return [(level, code, message)] for rules the schema cannot express. Assumes the shape is valid."""
    out = []
    ops = decl.get("operations", [])
    names = [o.get("name") for o in ops if isinstance(o, dict)]
    for n in sorted({n for n in names if names.count(n) > 1}):
        out.append(("error", "E-OP-DUPLICATE", f"operation name {n!r} is declared more than once"))
    for o in ops:
        if isinstance(o, dict) and o.get("approval") == "never":
            out.append(("error", "E-APPROVAL-NEVER", "approval 'never' does not exist: use 'always' or 'per_mode'. The core decides when approval is forced"))
    scope = decl.get("scope") or {}
    if scope.get("level") == "user" and not scope.get("user_filter_field"):
        out.append(("error", "E-SCOPE-USER-FIELD", "scope.level 'user' needs scope.user_filter_field (the field that holds the user id)"))
    net = decl.get("network") or {}
    hosts = list(net.get("hosts", []))
    tcp_hosts = [t.get("host") for t in net.get("tcp", []) if isinstance(t, dict)]
    for h in hosts + [x for x in tcp_hosts if x]:
        if _is_internal_host(h):
            out.append(("error", "E-NET-INTERNAL-HOST", f"host {h!r} is an address or an internal name; declare public host names only (private hosts belong in the owner's allow list)"))
    if any(isinstance(o, dict) and o.get("effect") == "external_send" for o in ops) and not (hosts or tcp_hosts):
        out.append(("error", "E-NET-REQUIRED", "an operation with effect 'external_send' needs network.hosts (or network.tcp)"))
    for s in decl.get("secrets", []):
        if s in PROTECTED_SECRETS:
            out.append(("error", "E-SECRET-PROTECTED", f"secret {s!r} belongs to the core and cannot be declared"))
    if net.get("tcp"):
        out.append(("warning", "W-TCP-NOT-ENFORCEABLE", "network.tcp is reserved: it cannot be enforced while the connector runs inside the RagLeap process (only the isolated host will enforce it)"))
    desc = decl.get("descriptive") or {}
    for c in desc.get("data_classes", []):
        if c not in KNOWN_DATA_CLASSES:
            out.append(("warning", "W-DATA-CLASS-UNKNOWN", f"data class {c!r} is not in the known list {sorted(KNOWN_DATA_CLASSES)}"))
    if desc.get("data_classes"):
        out.append(("info", "I-DESCRIPTIVE-ONLY", "descriptive.data_classes is shown to people and is NOT enforced by the core"))
    if "budgets" not in decl:
        out.append(("info", "I-NO-BUDGETS", "no budgets declared: the core's own limits apply"))
    return out


def check_declaration(decl, schema):
    """Return a list of findings (level, code, message) for one parsed declaration."""
    findings = [("error", "E-SCHEMA", f"{p}: {m}") for p, m in validate(decl, schema)]
    if isinstance(decl, dict):
        # semantic rules run even when the shape has problems, but only on parts that are well formed
        try:
            findings += semantic_findings(decl)
        except (AttributeError, TypeError):
            pass
    return findings
