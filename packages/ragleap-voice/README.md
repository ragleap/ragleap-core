# ragleap-voice

Open-source (MIT), local-first speech tooling, with Indian languages as a priority.

**Status: pre-alpha. Versions 0.1.0 and 0.2.0 add `transcribe()` and `synthesize()` with a Whisper large-v3 engine and an Indic Parler-TTS engine (see "What 0.1.0 adds" below). Every model is unverified and none is a default. Each engine has been run on only a few clips on one maintainer's machine. This is a first release for testing, not a finished product.**

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

## What 0.1.0 and 0.2.0 add

**0.2.0** adds opt-in number-word normalizers for the benchmark (Turkish with ordinals, Spanish, German, Italian, Indonesian, Brazilian Portuguese, Hindi, Malay; contributed by an outside volunteer, not reviewed by fluent speakers), standard-library audio helpers (not exported), an optional energy gate for `transcribe()` (off by default; measured to drop quiet real speech at its default floor), and a note on filler-phrase or empty output. Details and limits are in the changelog and `benchmarks/voice/README.md`.

**0.1.0** added:

Pre-alpha: names and signatures can still change. The core has no runtime dependencies. Engines are separate: the Whisper engine needs its extra (`pip install "ragleap-voice[whisper]"`; without it the call raises an error that names this command), and the Parler engine has no extra, because you set up its Python environment yourself (see issue 990 below).

- `transcribe()` and `synthesize()`, which check the licence, the lock and the verification status of every model before an engine sees it, and hand engines verified folders only. Every model is `unverified` and none is a default, so calls need `allow_unverified=True`.
- A Whisper large-v3 engine (optional extra `whisper`, faster-whisper). Run with the real model on two clips (English and Spanish) on a CPU, and on one Colab T4 with NVIDIA's cublas and cudnn pip packages preloaded by hand (the engine does not preload them). Other GPUs and CUDA versions have not been tried. Input must be mono 16-bit 16 kHz WAV.
- An Indic Parler-TTS engine, run as an isolated worker from a Python environment you set up yourself (`RAGLEAP_VOICE_PARLER_PYTHON`). Run once with the real model on a T4 for Hindi and English. The model is gated on Hugging Face. The `parler-tts` library is installed from GitHub and is not covered by the lock files, and the versions of its dependencies are not pinned by us.
- No emotion names are declared. Style goes through `options={"description": "..."}`, and what that changes has not been measured. Voice cloning is not built.
- Isolation separates conflicting dependencies and keeps tokens out of the worker. It is not a security sandbox.

Help with testing is wanted: https://github.com/ragleap/ragleap-core/issues/960 (Whisper), https://github.com/ragleap/ragleap-core/issues/990 (Parler) and the roadmap at https://github.com/ragleap/ragleap-core/issues/970.

Install check (one maintainer run, not a guarantee): `ragleap-voice[whisper]` installed from PyPI on a fresh Colab T4 runtime and transcribed one clip each in seven more declared languages (see issue 960). One clip per language is not an accuracy measurement.

## Measurements

Reproducible benchmarks with their limits are published at https://github.com/ragleap/ragleap-core/tree/main/benchmarks/voice. They use small samples of read speech on one GPU. The WER/CER helper can optionally normalize English (`numbers=True`), Turkish (`numbers="tr"`) cardinal and ordinal words, Spanish (`numbers="es"`) cardinal words, German (`numbers="de"`) cardinal words, Italian (`numbers="it"`) cardinal words, Indonesian (`numbers="id"`), Brazilian Portuguese (`numbers="pt-BR"`), Hindi (`numbers="hi"`), or Malay (`numbers="ms"`) cardinal words; all options are off by default and have not been applied to the published tables. Italian supports standard written forms below `trilione` (normalizer version `1+numbers-it.2`), keeps adjacent standalone numbers separate, retains the decimal comma, and removes periods used as thousands separators when each group has three digits. Indonesian supports standard cardinals from zero through 999 triliun, treats period groups as thousands separators, and retains decimal commas. The ambiguous scale name `biliun` and larger scales are excluded. Its parser preserves malformed scale phrases and keeps adjacent standalone cardinals separate; because it is context-free, common words such as `satu` in ordinary prose may still be interpreted as a number. Other language strings raise ValueError rather than falling back to English. In those tests an open Indian-language recogniser (IndicConformer) had clearly lower word error than Whisper large-v3 on Hindi, Tamil, Telugu, Malayalam and Kannada. Naturalness, emotion quality and comparisons with commercial services have not been measured, and this project makes no claim about them.

Brazilian Portuguese is available as `numbers="pt-BR"` (normalizer version `1+numbers-pt-BR.1`), opt-in and not applied to published tables. It supports standard standalone cardinals through 999,999,999,999,999,999 with Brazilian short-scale names through `quadrilhão` (also `quatrilhão`); malformed scale sequences and adjacent standalone values are preserved, decimal commas are retained, and only complete period-grouped numbers are normalized. Ordinals, fractions, spoken decimals, dates and currency-specific forms are out of scope. Since the normalizer has no grammatical context, cardinal words in ordinary prose can still be converted.

Hindi is available as `numbers="hi"` (normalizer version `1+numbers-hi.2`), opt-in and not applied to published tables. It supports Devanagari standalone cardinals through 99 खरब (9,999,999,999,999), with descending सौ, हज़ार/हजार, लाख, करोड़, अरब and खरब scales. CLDR, BIS, the Government of India Hindi coursebook, and the Hindi grammar reference differ on some spellings; the parser accepts only directly attested variants. It retains Hindi decimal points and removes commas only for complete Indian-style grouping. Ordinals, fractions, negatives, spoken decimals, dates, currencies, transliterations, collective forms and larger scales are excluded. Ordinal-containing phrases are kept intact, including inflected ordinal forms. The source-attested spelling `पांच` is accepted alongside `पाँच`; NFKC handles decomposed nukta in `करोड़`. It retains a terminal sentence comma or period only after validating the complete grouped number. The context-free parser may normalize cardinals used as ordinary prose.

Malay is available as `numbers="ms"` (normalizer version `1+numbers-ms.1`), opt-in and not applied to published tables. It supports standard standalone cardinals from `kosong`/`sifar` through 999,999,999,999,999,999, including `seribu`/`sejuta` and the Malaysian short-scale values `bilion` = 10⁹, `trilion` = 10¹², and `kuadrilion` = 10¹⁵. Because DBP dictionary material records conflicting long- and short-scale uses for `bilion`/`trilion`, this mode follows CLDR, the DBP school mathematics glossary, and DBP's short-scale numeric examples. It removes commas only from fully valid three-digit grouping and retains decimal points. Ordinals, dates, fractions, spoken negative forms, spoken decimals, currency, dialect/inflected forms, and scales at or above 10¹⁸ are excluded; malformed scale phrases remain unchanged. This context-free parser may still normalize cardinal homonyms used in ordinary prose.

## Planned, not available

Streaming, a voice-activity gate, resampling, voice profiles with a consent record, emotion and style control, a registry of community-trained models, verified models, and more languages and engines.

## Licence

MIT. Models are separate downloads with their own licences, listed in the manifest and in https://github.com/ragleap/ragleap-core/blob/main/packages/ragleap-voice/THIRD_PARTY_NOTICES.md

Source and issues: https://github.com/ragleap/ragleap-core/tree/main/packages/ragleap-voice
