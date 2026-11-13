# Supply chain of the core image

What a core release tag (`vX.Y.Z`) or a manual run of the "Publish app image" workflow produces for `ghcr.io/ragleap/ragleap-app`, and what it does not. The image package is private for now.

## What is published

- **The image**, built from the repo `Dockerfile` with the exact package versions of `requirements.lock`.
- **Build attestations** stored with the image: provenance (how and from which commit it was built) and an SBOM of what is inside it.
- **A keyless signature** (cosign) made by the workflow run itself, on the image digest. No signing key exists to steal; the signature records the GitHub identity of the run.
- **A CycloneDX SBOM** of the pinned Python packages (`ragleap-core-sbom.cdx.json`), attached to the GitHub release on tag runs and kept as a workflow artifact for 90 days.
- **A Trivy scan** of the image in the run log. It is report only and never stops a release.

## Check a signature

Log in to the registry first (the package is private), then, with the digest of the image you pulled:

    cosign verify ghcr.io/ragleap/ragleap-app@sha256:DIGEST \
      --certificate-identity-regexp 'https://github.com/ragleap/ragleap-core/.github/workflows/publish-image.yml@refs/tags/v.*' \
      --certificate-oidc-issuer https://token.actions.githubusercontent.com

A manual run is signed with its branch identity instead of a tag, so it does not match the pattern above.

## What this does not say

- A signature says who built the image and from which workflow, not that it is free of vulnerabilities or bugs. The Trivy report can show findings; read it before you rely on an image.
- The CycloneDX file lists Python packages only. The SBOM attestation stored with the image describes the image contents.
- There is no AI bill of materials, and no claim of a SLSA level or of any compliance with a law or standard.
- Two builds from the same lock file install the same package versions; the images are not byte-identical.
- The workflow was added without a live run. The first manual run after the change is its test.
