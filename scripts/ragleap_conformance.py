#!/usr/bin/env python3
"""Local conformance checker for RagLeap capability declarations (schema ragleap.capability/1, draft).

Runs offline and needs nothing but Python 3: it reads docs/capability-schema-v1.schema.json (the one source of truth)
with a small built-in validator, then applies the semantic rules the schema cannot express. Optionally it also lints
connector source files for direct use of the network, the environment or a subprocess.

Usage:
  python scripts/ragleap_conformance.py DECLARATION.json [MORE.json ...] [--source connector.py ...] [--strict] [--json]
  python scripts/ragleap_conformance.py --print-schema

Exit code: 0 conforms, 1 errors (or warnings with --strict), 2 the files could not be read.
Every message has a code (E-... error, W-... warning, I-... information). This checker validates the shape of a
declaration. The RagLeap core still applies its own policy: a declaration can only narrow it, never widen it.
"""
import argparse
import ipaddress
import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_SCHEMA = HERE.parent / "docs" / "capability-schema-v1.schema.json"
sys.path.insert(0, str(HERE))

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


def check_source(path: pathlib.Path):
    """Lint one connector source file: no direct network, environment, subprocess or dynamic import."""
    import ragleap_boundary_scan as scan
    viol, watch = scan.scan_connector(path.read_text(encoding="utf-8"))
    out = [("error", "E-SRC-DIRECT-ACCESS", f"{v}: use HostServices instead of direct access") for v in viol]
    out += [("warning", "W-SRC-DRIVER", f"{w}: a database or cloud driver opens its own connections; it will need the 'tcp' primitive on the isolated host") for w in watch]
    return out


# ---------------------------------------------------------------- command line

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("declarations", nargs="*", help="capability declaration JSON files")
    ap.add_argument("--source", action="append", default=[], help="connector source file to lint (repeatable)")
    ap.add_argument("--schema", default=str(DEFAULT_SCHEMA))
    ap.add_argument("--strict", action="store_true", help="warnings also fail")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--print-schema", action="store_true")
    a = ap.parse_args(argv)
    try:
        schema = json.loads(pathlib.Path(a.schema).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"cannot read the schema: {e}", file=sys.stderr)
        return 2
    if a.print_schema:
        print(json.dumps(schema, indent=2))
        return 0
    if not a.declarations and not a.source:
        ap.print_usage(sys.stderr)
        return 2
    report, failed = [], False
    for f in a.declarations:
        try:
            decl = json.loads(pathlib.Path(f).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"cannot read {f}: {e}", file=sys.stderr)
            return 2
        report.append((f, check_declaration(decl, schema)))
    for f in a.source:
        try:
            report.append((f, check_source(pathlib.Path(f))))
        except (OSError, SyntaxError) as e:
            print(f"cannot read {f}: {e}", file=sys.stderr)
            return 2
    for f, findings in report:
        if any(l == "error" for l, _, _ in findings) or (a.strict and any(l == "warning" for l, _, _ in findings)):
            failed = True
    if a.json:
        print(json.dumps([{"file": f, "findings": [{"level": l, "code": c, "message": m} for l, c, m in fs]} for f, fs in report], indent=2))
    else:
        for f, findings in report:
            n_err = sum(1 for l, _, _ in findings if l == "error")
            print(f"{f}: {'FAIL' if n_err else 'ok'} ({n_err} error(s), {sum(1 for l, _, _ in findings if l == 'warning')} warning(s))")
            for l, c, m in findings:
                print(f"  {l.upper():7} {c}: {m}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
