# Versions of ragleap-physical and what they promise

Pre-alpha, simulation only. Nothing here controls real hardware. A version number in this project is a label, not a safety claim.

This page says what each version range is meant to promise and what has to be true before the project uses the next label. It does not say that any condition is met today: a statement of that kind belongs in the claims ledger (`docs/claims.json`) with a run that backs it.

## 0.0.x: first public releases

The public surface can change between releases: the four tool names, the Observation format, the Proposal format and its digest, and the adapter contract. Every change is listed in `CHANGELOG.md`. The README states the status, and the status is pre-alpha.

## 0.1.0: the public surface is deliberate

The project may use 0.1.0 only when all of these are true:

1. The package installs and the test suite passes in the project's own CI on Linux, Windows and macOS, for every Python version the classifiers list. The Windows and macOS jobs are blocking, not informative.
2. The four tools, Observation v0, the Proposal format and digest, and the adapter contract are written down and tested as a contract.
3. No safety defect is open without an issue and a strict expected failure in the tests.
4. The Windows result has been repeated, by a second tester or by the project's own Windows CI over many runs, and the failures are counted and published.
5. One independent reviewer has read the bridge and the envelope and sent written findings.
6. A release to PyPI waits for a reviewer's approval on the release environment, and the files carry a provenance attestation.
7. The development status classifier moves from Pre-Alpha to Alpha, and the README still says simulation only.

## 1.0.0: a promise not to break you, and claims we can stand behind

The project may use 1.0.0 only when all of these are true:

1. A written compatibility promise and a deprecation rule exist.
2. Every invariant in `docs/safety-invariants.md` has a named test and a deliberate-break proof and, where an honest probe from outside can be written, a class in the red-team harness.
3. An independent safety and security review is finished, its findings are fixed and the result is published.
4. At least one outside user has run observe-only on real data and written down what happened.
5. The supported platforms are proven in CI: Linux, Windows and macOS, with the Python versions the classifiers list.
6. The README says what 1.0.0 does and does not mean: a stable interface for simulation and observe-only use, never "ready for real machines".
7. The release process is hardened: provenance, a software bill of materials and a supply-chain scan.

A 1.0.0 the project cannot honour is worse than a long 0.x series.

## What no version means

No version number is a safety certification, a statement about real hardware, or a permission to use the package where it could hurt people. `ragleap-physical` is never a safety function. Real hardware is not open for contributions. Out of scope, always: life-safety systems, medical devices, vehicles on public roads, weapons, and anything that could hurt people without an independent hardware safety stop.

## How a release is made

1. The tests run on the exact commit that will be tagged, with the versions written down.
2. The package is uploaded to TestPyPI first and its files are checked against git.
3. The changelog heading carries the real upload day in UTC.
4. The tag starts the upload to PyPI. A PyPI version number can never be reused and its files can never be edited, so a mistake costs a new version.
5. The files on PyPI are checked again against git and against the TestPyPI files, and the result is written down.
