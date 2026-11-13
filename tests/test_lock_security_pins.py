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


def test_the_packages_with_fixes_found_by_the_image_scan_are_at_fixed_versions():
    # Trivy on the published image (first run of the publish workflow) reported these as fixable: certifi (critical),
    # oauthlib, pyasn1 and pypdf (high). The minimum is the first fixed version.
    for name, minimum in (("certifi", (2026, 10, 18)), ("oauthlib", (4, 0, 1)), ("pyasn1", (0, 6, 5)), ("pypdf", (6, 21, 4))):
        v = pinned(name)
        assert v is not None and v >= minimum, "%s in requirements.lock must be %s or newer, found %s" % (name, minimum, v)
