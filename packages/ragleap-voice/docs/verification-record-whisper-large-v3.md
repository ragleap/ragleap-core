# Verification record: whisper-large-v3 (evidence for the maintainer's review, 2026-11-23)

Status: a record of evidence against `docs/verification-criteria.md`. The entry stays `unverified` and not default. Nothing here changes `models.toml` or the lock, and no criterion is signed off: criterion 8 belongs to the maintainer. Tags: [enforced], [read], [measured] (our own run, one run unless stated), [unverified].

## Evidence by criterion

1. **Lock matches** [enforced]: `locks/whisper-large-v3.json` lists 7 files at revision `edaa852ec7e145841d8ffdb056a99866b5f0a478`. On 2026-11-23 `models pull whisper-large-v3 --allow-unverified` verified and installed all 7 files into an empty cache [measured].
2. **Safe formats** [enforced]: `model.bin` is tagged ctranslate2 and passes the header check (`docs/design/whisper-format-exception.md`). No other pickle-capable file is listed.
3. **Pinned revision, sizes, SHA-256** [enforced]: the same pull checked every file against the lock.
4. **Allowlist** [enforced]: the entry names MIT for code and weights, both on the allowlist. The other reading in criterion 5 (Apache-2.0) is also on the allowlist.
5. **Licence read at the source** [read 2026-11-23, page extracts only]: the page https://huggingface.co/Systran/faster-whisper-large-v3 declares `license: mit`; its latest commit is `edaa852`, the pinned one, and its README is 2,052 bytes like the lock's; the card says the repository is a conversion of the upstream model `openai/whisper-large-v3`. The page https://huggingface.co/openai/whisper-large-v3 declares `license: apache-2.0`. The two cards disagree. No LICENSE file was read in either repository, nor the upstream project's own repository licence. Training-data rights: not stated on the cards read, so unknown. A person still has to read the licence files and decide which licence the entry names. This is a reading, not legal advice.
6. **Reproducible benchmark with limits** [documented, not re-run]: `benchmarks/voice/README.md` has the sections "Whisper large-v3 baseline", "Five Indian languages, 60 clips each" and "Whisper large-v3 on 12 languages (pilot)", each with its limits (Colab T4, 60 FLEURS clips per language, 20 for English). They used faster-whisper 1.1.1, while the extra pins 1.2.1. The scripts were not re-run for this record.
7. **Fresh environments, two Python versions** [measured, laptop CPU, one run each]: `ragleap-voice[whisper]==0.3.6` from PyPI into a new venv with Python 3.12.3 and another with Python 3.10.22 (faster-whisper 1.2.1, ctranslate2 4.9.1), the model pulled into an empty cache, then `transcribe()` on a 2.2 s synthetic Hindi clip with `language="en"` and `options={"device": "cpu"}` (int8). Both ran: first call 60.1 s (3.12) and 46.4 s (3.10), peak memory about 3.0 GB, `model_status` unverified. The two texts differ by one spelling. This shows that the model loads and runs. It is not an accuracy test.
8. **Maintainer sign-off**: not done.

## What bears on making it a default

- The engine declares only `en, es, fr, id, ja, pt, ru, tr, vi` (languages with a usable measured result in `coverage/stt.csv`) [read: whisper_engine.py of 0.3.6]. With this engine `transcribe()` refuses Hindi, Tamil, Telugu, Malayalam and Kannada, and `language=None` fails because the engine does not detect the language [measured]. In our own measurement Whisper's word error rate on 60 FLEURS clips was 26.8% in Hindi, 49.3% in Tamil, 73.9% in Telugu, 67.2% in Kannada and 111.4% in Malayalam; IndicConformer was lower in all five, but it is gated, loads code from its own repository and is not eligible as a default. A verified Whisper default would therefore give speech recognition for nine languages, none of them Indian.
- The entry still has `languages = []`, `capabilities = ["streaming:unknown"]` and `min_hardware = "unknown"`.
- Not tested: `device="auto"` on a machine without a GPU (the library chooses the compute type and may need far more memory than int8); the GPU path of 0.3.6 outside Colab; Windows and macOS; Python 3.11; accuracy on any language through this install.

## For the maintainer

Read the licence files and decide which licence the entry names; decide whether to fill the empty fields before verification and whether to set `default`; sign off criteria 1 to 7 in the pull request that edits `models.toml`.

Update 2026-11-24: the sweep over 65 more languages (benchmarks/voice/README.md) gave 81 measured rows, and a written rule adopted on 2026-11-24 (open to revision) declares 31 languages instead of the 9 listed above (still none of the Indian languages: Hindi 26.8% word error, upper interval end 29.4%). The record above stays as written on 2026-11-23.
