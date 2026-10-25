# A justified exception to the pickle-format rule for Whisper (CTranslate2) weights (draft)

Status: the exception is implemented (checked 2026-10-25): `FORMAT_EXCEPTIONS` and the header check in `locks.py`, the `--format` option in `cli.py` and `pull.py`, and `tests/test_format_exception.py`. No model is `verified` yet; the verification criteria for speech-to-text are still undecided (section 6, item 5).
Any change to `models.toml` or `locks/*.json` needs the maintainer's personal review.

## 1. Problem

A verified or default model may not list pickle-capable files. `UNSAFE_SUFFIXES` in `locks.py` includes `.bin`.
The CTranslate2 weights of Whisper large-v3 are a single file named `model.bin`, so Whisper cannot be locked as verified.

Second problem: the `whisper-large-v3` entry in `models.toml` has `source_url = https://huggingface.co/openai/whisper-large-v3` and an empty revision.
Our benchmarks used `Systran/faster-whisper-large-v3` at revision `edaa852ec7`. The entry must point at the repository we actually run, at a full pinned revision, before it is locked.

## 2. Finding

The first 64 bytes of `model.bin` at `edaa852ec7` (hex):

    060000000c00576869737065725370656300030000001604000013006465636f6465722f61637469766174696f6e00000101000000030e006465636f6465722f

They show a little-endian uint32 `6`, a uint16 length `12`, the NUL-terminated name `WhisperSpec`, then further header fields.
There is no pickle protocol marker (0x80) and no zip marker (PK). This is consistent with CTranslate2's own binary format.
Only the header was read. This is evidence about the file's shape, not proof that the whole file is safe.

## 3. Proposal

1. **No global relaxation.** `.bin` stays unsafe by default.
2. **A format tag in the lock.** A file entry may carry `"format": "ctranslate2"`. The tag is inside the lock file, so the lock digest in `models.toml` covers it, and CODEOWNERS makes changes to `locks/` visible.
3. **A closed table in code**, for example `FORMAT_EXCEPTIONS = {"ctranslate2": ".bin"}`. A tag is honoured only for the suffix listed for it. Any new format needs a code change and the maintainer's review.
4. **A positive header check**, not a pickle blacklist: little-endian uint32 version, a uint16 name length, an ASCII name that ends in NUL and in `Spec`. Anything else is refused.
5. **When it runs:** `models lock` (refuses to write the tag if the check fails) and `models verify` (header check in addition to size and SHA-256). `models check` accepts a verified entry with unsafe-suffix files only if every one carries a known tag.
6. **No exception for `.pt`, `.pth`, `.pkl`, `.pickle`, `.ckpt`, `.joblib`, `.dill`.** Kokoro's weights stay unverifiable under this rule.

## 4. What this does not guarantee

- The header check does not prove the rest of the file is well formed.
- CTranslate2 reads the file with native code. A parser bug triggered by a crafted file is conceivable, even though the format holds data and not code.
- The hash pins the file that was reviewed at lock time. It does not say the file is trustworthy. We rely on the pinned revision of the upstream repository.
- The README must state this residual risk next to the exception.

## 5. Tests (planned, fakes only)

- A small fake file with a valid CTranslate2-shaped header and tag `ctranslate2`: accepted.
- Tagged `.bin` starting with 0x80, or with `PK`: refused.
- Untagged `.bin`: refused. Tag on a `.pt` file: refused. Unknown tag: refused.
- A verified entry whose lock tags every unsafe file passes `models check`; one untagged file fails it.
- No real weights in the test suite.

## 6. Order of work

1. Done: the Whisper entry in `models.toml` points at `Systran/faster-whisper-large-v3` with a full revision.
2. Done: implemented, with tests in `tests/test_format_exception.py`.
3. Build the lock on Colab (the weights are about 3 GB; never on the production VPS).
4. Done on 2026-10-25 in a Colab run: the Hub's SHA-256 and size of `model.bin` matched the lock (`69f74147...`, 3087284237 bytes). It was reported in issue #960 and needs the maintainer's review.
5. Verification criteria for a speech-to-text model are not defined yet and are a separate decision.

## 7. Open questions

1. Settled in practice: `"format"` is an optional key inside lock schema 1, and `validate_lock` accepts it only with a known tag that matches the file suffix.
2. Is a header check enough, or should the table also pin the expected spec name (`WhisperSpec`) per model?
