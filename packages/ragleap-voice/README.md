# ragleap-voice

Open-source (MIT), local-first speech tooling, with Indian languages as a priority.

**Status: pre-alpha. Version 0.0.2 on PyPI is the model-management layer only. There are no speech engines in that release: you cannot transcribe or synthesize audio with it. The repository's main branch already has more (see "On main, not released" below); none of it is on PyPI yet.**

## What works today

A command line and a small standard-library-only Python core (no runtime dependencies, Python 3.10 or newer) for finding, locking, downloading and verifying speech models safely:

    pip install ragleap-voice
    uv pip install ragleap-voice
    uvx ragleap-voice models list
    ragleap-voice models list
    ragleap-voice models show indic-parler-tts
    ragleap-voice models check

- `models list`, `show` and `check` read a manifest of candidate models with their licences, pinned revisions and status. Every entry is currently `unverified`: a research lead, not an endorsement. None is a default.
- Lock files record the size and SHA-256 of every file at a pinned revision. `models verify` checks a cache folder against them (offline, read-only). `models check` rejects verified or default entries that lack a matching lock, use licences outside the allowlist, or list pickle-capable files (formats that can run code when loaded).
- `models pull NAME` downloads from Hugging Face only: HTTPS, a host allowlist, and the access token (`HF_TOKEN`) is sent only to huggingface.co. Every file is checked against its lock before anything enters the cache, and `--offline` makes no network calls. Entries can declare dependencies, which are pulled first.
- `models lock NAME` is a maintainer tool that builds a lock by downloading and hashing every file.
- Two entries are locked so far: `indic-parler-tts` (a gated Hugging Face model: accept its conditions on the website and set `HF_TOKEN`) and its tokenizer dependency `flan-t5-large-tokenizer`. Both are pinned but have not had a human review, so pulling them needs `--allow-unverified`.

Nothing in the package uses the network unless you run `models pull` or `models lock`.

## On main, not released

Not in the PyPI release. Install from a checkout of the repository to try it. Pre-alpha: names and signatures can still change.

- `transcribe()` and `synthesize()`, which check the licence, the lock and the verification status of every model before an engine sees it, and hand engines verified folders only. Every model is `unverified` and none is a default, so calls need `allow_unverified=True`.
- A Whisper large-v3 engine (optional extra `whisper`, faster-whisper). Run with the real model on two clips (English and Spanish) on a CPU, and on one Colab T4 with NVIDIA's cublas and cudnn pip packages preloaded by hand (the engine does not preload them). Other GPUs and CUDA versions have not been tried. Input must be mono 16-bit 16 kHz WAV.
- An Indic Parler-TTS engine, run as an isolated worker from a Python environment you set up yourself (`RAGLEAP_VOICE_PARLER_PYTHON`). Run once with the real model on a T4 for Hindi and English. The model is gated on Hugging Face. The `parler-tts` library is installed from GitHub and is not covered by the lock files, and the versions of its dependencies are not pinned by us.
- No emotion names are declared. Style goes through `options={"description": "..."}`, and what that changes has not been measured. Voice cloning is not built.
- Isolation separates conflicting dependencies and keeps tokens out of the worker. It is not a security sandbox.

Help with testing is wanted: https://github.com/ragleap/ragleap-core/issues/960 (Whisper), https://github.com/ragleap/ragleap-core/issues/990 (Parler) and the roadmap at https://github.com/ragleap/ragleap-core/issues/970.

## Measurements

Reproducible benchmarks with their limits are published at https://github.com/ragleap/ragleap-core/tree/main/benchmarks/voice. They use small samples of read speech on one GPU. The WER/CER helper can optionally normalize English (`numbers=True`) or Turkish (`numbers="tr"`) cardinal-number words; it is off by default and has not been applied to the published tables. Other language strings raise ValueError rather than falling back to English. In those tests an open Indian-language recogniser (IndicConformer) had clearly lower word error than Whisper large-v3 on Hindi, Tamil, Telugu, Malayalam and Kannada. Naturalness, emotion quality and comparisons with commercial services have not been measured, and this project makes no claim about them.

## Planned, not available

Streaming, a voice-activity gate, resampling, voice profiles with a consent record, emotion and style control, a registry of community-trained models, verified models, and more languages and engines.

## Licence

MIT. Models are separate downloads with their own licences, listed in the manifest and in https://github.com/ragleap/ragleap-core/blob/main/packages/ragleap-voice/THIRD_PARTY_NOTICES.md

Source and issues: https://github.com/ragleap/ragleap-core/tree/main/packages/ragleap-voice
