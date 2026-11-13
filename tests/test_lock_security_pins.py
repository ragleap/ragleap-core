"""Pins in requirements.lock (what the Docker image installs) that must stay at or above a patched version."""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent


def pinned(name):
    for line in (ROOT / "requirements.lock").read_text(encoding="utf-8").splitlines():
        m = re.fullmatch(r"%s==([0-9][0-9A-Za-z.]*)" % re.escape(name), line.strip())
        if m:
            return tuple(int(p) for p in re.findall(r"\d+", m.group(1))[:3])
    return None


def test_soupsieve_is_at_a_patched_version():
    # Dependabot alerts on the ragleap-rag lock file: versions before 4.0.0 have a slow-down (denial of service) in
    # selector matching (fixed in 4.0.0), and versions up to 2.9.2 another one (fixed in 2.10.0).
    v = pinned("soupsieve")
    assert v is not None and v >= (4, 0, 0), "soupsieve in requirements.lock must be 4.0.0 or newer, found %s" % (v,)
