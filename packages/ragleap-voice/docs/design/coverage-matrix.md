# Language coverage matrix: every region, measured one language at a time (draft)

Status: draft for discussion. Nothing below is built; every item is **planned**.
Goal (from the project mission): cover Indian, Asian, African, European, North American and South American
languages as far as possible. India is where the first measurements exist; it is not the limit.

## 1. Problem

- `models.toml` has a `languages` field with no defined meaning. Whisper has an empty list, Chatterbox lists
  `hi` and `en` although its model card claims 23 languages. A card claim, a tested language and a measured
  language are three different facts and must not share one field.
- Measurements exist for six languages. Everything else is "not measured", but nothing in the repo says so in a
  machine-readable way, so `engine="auto"` (see api.md) has no data to route on.
- Outside contributors (issues #753, #758, #759) have no concrete row to claim.

## 2. The matrix is data

One file per kind, under `packages/ragleap-voice/coverage/` (`stt.csv`, `tts.csv`), one row per
(language, engine). It is plain CSV, readable by the standard library, shipped in the wheel.

Columns (planned):

| column | meaning |
|---|---|
| `language` | BCP 47 tag, for example `hi`, `sw`, `pt-BR` |
| `engine` | manifest entry name |
| `card_claim` | yes or no: the model card lists this language |
| `weights_licence`, `licence_checked_at` | licence and the URL where it was checked, with the date |
| `tier` | see section 3 |
| `metric`, `value`, `ci_low`, `ci_high` | WER or CER with the interval; empty if not measured |
| `test_set`, `n_clips`, `normalizer_version` | what was measured, and how |
| `evidence` | path to the raw result file under `benchmarks/voice/` |
| `hardware`, `package_versions` | reproducibility |
| `champion` | GitHub handle of the contributor who owns the row, if any |

A row without `evidence` cannot have a tier above 1.

## 3. Tiers

- **0, no open engine known.** Written down so the gap is visible.
- **1, engine exists, not measured.** The card claims the language and the licence is allowlisted.
- **2, measured.** A reproducible result with an interval is committed.
- **3, reviewed.** A second person re-ran or reviewed the result, and noisy or telephone audio was also measured.

Numeric thresholds for "good enough" are not set here. They are an open question (section 7).

## 4. Rules

1. A language appears in the README, a release note or a post only through a matrix row, with its tier.
2. A "not measured" row says so. Silence is not allowed.
3. Weights licence must be on the allowlist for a row to be used by `engine="auto"`. Models that are non-commercial,
   research-only or custom-licensed may be listed at tier 0 or 1 with the licence shown, and are never used by `auto`.
4. The `languages` field in `models.toml` becomes `card_languages` (what the card says). The matrix holds what we measured.
5. A CI test checks the files: valid tags, known engines, every `evidence` path exists, no tier above 1 without evidence,
   no row for an engine missing from the manifest.

## 5. Routing

`engine="auto"` reads the matrix: among installed engines that support the language, prefer the best measured
row (lowest error, tier 2 or higher), then tier 1 in manifest order with a `notes` line saying nothing was measured.
Each routing decision cites its row. No row means `NoEngineError`, not a guess.

## 6. Filling it

1. One runner that takes a language list, a test set and an engine (the current Colab scripts are hard-coded to five
   languages). It writes the raw results and one matrix row per run.
2. FLEURS (CC-BY-4.0) is the shared backbone because it spans many languages and regions. The exact number and list of
   languages is [unverified] and must be checked at the source before any claim.
3. First sweep: Whisper large-v3 on every FLEURS language that Whisper claims, on a free Colab GPU. The result shows where the
   current default is weak, and which languages need a better open model.
4. For each weak language: search for a permissively licensed model, check the licence at its source, run the same
   runner, record the row, including negative results.
5. Contributors claim rows by language and engine. One champion per row, and a quiet claim may be taken by someone else.

## 7. Open questions

1. Tier thresholds: what error rate makes a language "usable" for speech to text and for text to speech?
   Proposed 2026-11-24, not adopted: a language is declared for the Whisper engine when its tier 2 row has at least 60 clips and the
   upper end of its 95% interval is at or below 15% (word error rate, or the recomputed character error rate for languages without
   word spaces). Applied to the 81 Whisper rows this gives 31 languages, and a test derives the declared set from stt.csv. One
   threshold for both metrics is a simplification, and what is acceptable depends on the use.
2. Text to speech has no word error rate. Round-trip WER measures intelligibility only (the current method). Naturalness and
   emotion need the listening panel (#756). Which metric makes a tier 2 row for TTS?
3. FLEURS is read speech. Which test set covers telephone and noisy audio per language?
4. Low-resource languages with no permissively licensed open model: do we list research-licensed models at tier 0 or 1 for
   information only, or leave them out entirely?
5. Does a fine-tuned model published by a contributor on Hugging Face get its own matrix row with its own evidence?

## 8. First data (implementation notes)

- Files: `coverage/stt.csv` and `coverage/tts.csv`, checked by `ragleap_voice.coverage` and by the test suite. They ship in the wheel.
- Columns are fewer than sketched in section 2: licences stay in `models.toml` for manifest engines, and a short `weights_licence` text is kept for candidates. A `raw_committed` column and a `contamination` column were added.
- Models that are not in `models.toml` are written as `candidate:NAME` and are never used by engine selection.
- Until raw per-clip results are committed, tier 2 means reproducible by script and not independently reviewed.
- Text to speech rows stay at tier 1 (open question 2). No tier 0 rows exist yet, because "no open engine known" needs its own research.
