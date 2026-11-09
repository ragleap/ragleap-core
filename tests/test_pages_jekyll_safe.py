"""GitHub Pages builds this repository with Jekyll (see _config.yml).

Jekyll treats any file whose first line is "---" as a page and runs Liquid over it. A Jinja file
(for example Ansible YAML with "{{ ... }}") that starts with "---" therefore breaks the whole site
build ("Liquid error ... wrong number of arguments"). Such files must be listed under "exclude" in
_config.yml, so these tests fail early instead of the Pages deployment failing later.
"""
import fnmatch
import os
import re

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEADER = re.compile(rb"\A---\s*\r?\n")
LIQUID = re.compile(rb"\{\{|\{%")


def _config():
    with open(os.path.join(ROOT, "_config.yml"), encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _excluded(rel, patterns):
    for p in patterns:
        p = p.rstrip("/")
        if rel == p or rel.startswith(p + "/") or fnmatch.fnmatch(rel, p):
            return True
    return False


def _risky_files():
    patterns = [str(p) for p in (_config().get("exclude") or [])]
    found = []
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if not d.startswith(".") and not (d.startswith("_") and d != "_layouts")
                   and d not in ("node_modules", "vendor")]
        for name in files:
            if name.startswith((".", "_")):
                continue
            path = os.path.join(base, name)
            rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
            if _excluded(rel, patterns):
                continue
            try:
                with open(path, "rb") as f:
                    head = f.read(16)
                    if not HEADER.match(head):
                        continue
                    data = head + f.read()
            except OSError:
                continue
            if LIQUID.search(data):
                found.append(rel)
    return sorted(found)


def test_no_liquid_syntax_in_files_jekyll_would_render():
    risky = _risky_files()
    assert risky == [], (
        "These files start with '---' and contain '{{' or '{%', so the GitHub Pages (Jekyll) build "
        "would try to render them with Liquid and can fail. Add them to 'exclude' in _config.yml: "
        + ", ".join(risky)
    )


def test_ansible_playbooks_are_excluded_from_pages():
    patterns = [str(p).rstrip("/") for p in (_config().get("exclude") or [])]
    assert "packages/ragleap-ansible/playbooks" in patterns
