# When may a model be marked `verified`? (criteria, proposal)

Status: pre-alpha. Every model in `models.toml` is `unverified` and none is a default. This note says what has to be true before an entry changes to `verified`. Nothing here is a quality claim about any model.

Tags: **[enforced]** checked by code or CI today; **[proposed]** a human step this project intends to require, not enforced by code.

## Criteria

1. **A lock file exists and matches.** `locks/<name>.json` exists, is well formed, and its digest equals the `sha256` field of the entry. [enforced: `manifest.py`, `lock_problems`]
2. **Safe file formats only.** The lock lists no pickle-capable files. The one closed exception is a CTranslate2 `.bin` with a header check (see `docs/design/whisper-format-exception.md`). [enforced]
3. **Pinned revision, per-file size and SHA-256.** `models verify` compares a downloaded folder with the lock, offline and read-only. [enforced]
4. **Licence on the allowlist.** Code and weights licences are on the allowlist (MIT, Apache-2.0, BSD, ISC, CC-BY-4.0 with attribution). [enforced: manifest validation in CI]
5. **Licence text read at the source.** A person read the exact licence text at the model's source and recorded the date and the link. Training-data rights are noted where known and marked unknown where not. [proposed]
6. **A reproducible benchmark with its limits.** A script under `benchmarks/voice/` reproduces a result, and the limits (sample size, language, hardware) are stated next to it. No "better than" claim without one. [proposed]
7. **Runs in a clean environment.** The model loads and runs from a fresh virtual environment on at least two Python versions, recorded with versions and dates. [proposed]
8. **Maintainer sign-off.** The maintainer personally reviews criteria 1 to 7 and approves the change in the pull request that edits `models.toml`. That file is protected, so the maintainer merges it. [proposed, required]

## What `verified` does not mean

It does not mean the model is accurate, safe for a given use, free of bias, or legally cleared for any particular deployment. It means the files are the pinned ones and the rules above were followed.

## Current state

`whisper-large-v3`: the pinned file hashes match the lock; criteria 5 to 8 are not done. It stays `unverified`.
