"""The publish-image workflow: signed by digest, attestations on, report-only scan, least privilege, pinned actions,
never from a pull request, and the page that explains how to check it."""
import pathlib
import re

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows" / "publish-image.yml"


@pytest.fixture(scope="module")
def wf():
    return yaml.safe_load(WF.read_text(encoding="utf-8"))


def steps(job):
    return job["steps"]


def test_it_runs_only_on_release_tags_or_by_hand(wf):
    on = wf.get(True) or wf.get("on")
    assert set(on) == {"push", "workflow_dispatch"} and on["push"] == {"tags": ["v*"]}


def test_every_action_is_pinned_to_a_commit(wf):
    for name, job in wf["jobs"].items():
        for s in steps(job):
            if "uses" in s:
                assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", s["uses"]), (name, s["uses"])


def test_the_two_jobs_have_least_privilege(wf):
    assert set(wf["jobs"]) == {"publish-app-image", "core-sbom"}
    assert wf["permissions"] == {"contents": "read", "packages": "write", "id-token": "write"}
    assert wf["jobs"]["core-sbom"]["permissions"] == {"contents": "write"}
    assert "permissions" not in wf["jobs"]["publish-app-image"]


def test_the_image_gets_attestations_and_a_signature_by_digest(wf):
    job = steps(wf["jobs"]["publish-app-image"])
    ids = [s.get("id") for s in job]
    build = job[ids.index("build")]
    assert build["with"]["provenance"] == "mode=max" and build["with"]["sbom"] is True and build["with"]["push"] is True
    uses = [s.get("uses", "") for s in job]
    inst = next(i for i, u in enumerate(uses) if u.startswith("sigstore/cosign-installer@"))
    sign = next(i for i, s in enumerate(job) if "cosign sign" in s.get("run", ""))
    assert ids.index("build") < inst < sign
    assert job[sign]["env"]["DIGEST"] == "${{ steps.build.outputs.digest }}"
    assert 'cosign sign --yes "ghcr.io/ragleap/ragleap-app@${DIGEST}"' in job[sign]["run"] and "exit 1" in job[sign]["run"]


def test_the_scan_is_after_the_signature_report_only_and_cannot_undo_it(wf):
    job = steps(wf["jobs"]["publish-app-image"])
    sign = next(i for i, s in enumerate(job) if "cosign sign" in s.get("run", ""))
    scan = next(i for i, s in enumerate(job) if s.get("uses", "").startswith("aquasecurity/trivy-action@"))
    assert scan > sign and job[scan]["continue-on-error"] is True and job[scan]["with"]["exit-code"] == "0"
    assert "steps.build.outputs.digest" in job[scan]["with"]["image-ref"]
    assert job[scan]["env"]["TRIVY_PASSWORD"] == "${{ secrets.GITHUB_TOKEN }}"


def test_the_sbom_job_uses_the_lock_file_and_attaches_only_on_tags(wf):
    job = steps(wf["jobs"]["core-sbom"])
    gen = next(s for s in job if "cyclonedx-py" in s.get("run", ""))
    assert "requirements.lock" in gen["run"] and "cyclonedx-bom==" in gen["run"]
    attach = next(s for s in job if "gh release upload" in s.get("run", ""))
    assert attach["if"] == "startsWith(github.ref, 'refs/tags/v')" and "--clobber" in attach["run"]
    assert any(s.get("uses", "").startswith("actions/upload-artifact@") for s in job)


def test_the_docs_page_explains_how_to_check_and_what_it_does_not_say():
    text = (ROOT / "docs" / "supply-chain.md").read_text(encoding="utf-8")
    assert "cosign verify" in text and "certificate-identity-regexp" in text and "token.actions.githubusercontent.com" in text
    assert "report only" in text and "does not say" in text and "without a live run" in text
    assert "{{" not in text and "{%" not in text
