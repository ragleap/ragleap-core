## What does this PR do?

Briefly describe the change.

## Related issue

Closes #

## Checklist

- [ ] I've tested this change locally
- [ ] I've updated documentation if needed
- [ ] My changes follow the existing code style

## Connectors and tools (only if this PR adds or changes one)

Until process isolation is available, every connector that runs inside the RagLeap process is trusted code. See CONTRIBUTING.md.

- [ ] I ran `python scripts/ragleap_conformance.py` on its declaration
- [ ] It does not use the network, the environment or a subprocess directly (`python scripts/ragleap_boundary_scan.py` passes)
- [ ] This PR says what was tested and what was not, and makes no "secure", "certified" or "production-ready" claim without evidence
