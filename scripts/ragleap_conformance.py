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
import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_SCHEMA = HERE.parent / "docs" / "capability-schema-v1.schema.json"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))   # the repository root, so that core.connector_host can be imported

from core.connector_host.schema import (  # noqa: E402,F401  (re-exported: the checker and its tests use these names)
    INTERNAL_SUFFIXES, KNOWN_DATA_CLASSES, PROTECTED_SECRETS, SCHEMA_V1, check_declaration, semantic_findings, validate)


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
