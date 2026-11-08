# Voice benchmarks

Measurements for ragleap-voice and the script that produced them. Nothing here is a marketing claim.

## Whisper large-v3 baseline (first measurement, small sample)

| Language | Clips | WER | CER | RTF | Audio |
|---|---|---|---|---|---|
| English (en_us) | 20 | 4.8% | 2.2% | 0.09 | 189 s |
| Hindi (hi_in) | 20 | 25.4% | 7.3% | 0.24 | 235 s |
| Tamil (ta_in) | 20 | 43.9% | 11.2% | 0.20 | 277 s |
| All | 60 | 23.3% | 7.2% | 0.18 | 700 s |

## Method

- Every JSON result file records its normalizer version in `meta.normalizer_version`; files written before this field existed use version `1` (the default normalizer).
- Data: first 20 clips per language from the `test` split of `google/fleurs` (CC-BY-4.0). Audio is downloaded at run time and is not committed.
- Engine: faster-whisper 1.1.1, model `Systran/faster-whisper-large-v3` at revision `edaa852ec7` (MIT), fp16, beam 5, temperature 0, language given.
- Hardware: Tesla T4 (15 GB), Google Colab. Run on 2026-10-11. Harness commit on main: `6d9fbecd0c`.
- Metrics: corpus-level WER and CER (total errors over total reference units), one shared normalizer (`ragleap_voice.bench.metrics`, version `1`: the default, without the number step). CER counts Unicode code points. RTF is time spent divided by audio length (below 1 is faster than real time); the first call is a warm-up and is not timed.

## Limits (read before quoting any number)

- 20 clips per language is 3 to 5 minutes of audio. There are no confidence intervals. Treat the numbers as rough.
- Few systems, one run each. Two open systems are compared on Hindi and Tamil only (next section). Nothing here supports a claim about any commercial service.
- The normalizer used here (version `1`) does not unify digits and spelled-out numbers. Optional number normalization is available as `numbers=True` (English, version `1+numbers-en.1`) and `numbers="tr"` (Turkish cardinal and ordinal numbers, version `1+numbers-tr.2`); it also maps decimal digits of any script to ASCII. Neither option was used for a number in the tables on this page, and results measured with either option are not comparable with them. The Turkish mode covers cardinal numbers through katrilyon and standalone ordinal forms built from those components; dates, currency-specific handling, dialect and unrelated inflections are out of scope. Unsupported language strings raise ValueError rather than falling back to English; no third-party number-word data is used. WER on agglutinative languages such as Tamil is inflated by one wrong suffix. CER is the fairer figure for Indic scripts.
- GPU speed only. CPU speed and time to first partial result are not measured.
- Reference transcripts are used as given by FLEURS and were not re-checked.
- The per-clip raw results (CSV and JSON) are not committed yet. They are produced by the script below and will be added in a later change. Until then the table above comes from the printed run summary.

## IndicConformer-600M vs Whisper large-v3 (Hindi and Tamil only)

Second measurement on the same 40 clips (the first 20 Hindi and first 20 Tamil FLEURS test clips) with the same normalizer. IndicConformer has no English, so English is not part of this comparison. Whisper numbers are the ones above.

| 20 clips each | Whisper large-v3 | IndicConformer CTC | IndicConformer RNNT |
|---|---|---|---|
| Hindi WER | 25.4% | 9.5% | 8.2% |
| Hindi CER | 7.3% | 2.6% | 2.4% |
| Tamil WER | 43.9% | 29.8% | 26.0% |
| Tamil CER | 11.2% | 12.6% | 12.2% |
| Hindi RTF | 0.242 | 0.033 | 0.085 |
| Tamil RTF | 0.201 | 0.012 | 0.068 |

What it shows:

- Hindi: IndicConformer's word error rate is about a third of Whisper's, and its character error rate is lower too.
- Tamil: word error rate is lower, but character error rate is not lower than Whisper's (12.2% against 11.2% for RNNT). In one inspected clip the reference has the Latin letters "ph"; Whisper wrote Latin "pH" and IndicConformer spelled it phonetically in Tamil script. Latin-script tokens may affect Tamil CER, but only one clip was inspected, so this is a hypothesis, not a finding.
- RNNT decoding is slightly more accurate than CTC and several times slower.

Method:

- Model `ai4bharat/indic-conformer-600m-multilingual` at revision `2a77d305038dfc502fd9464e7d028b90381976cb` (MIT per the model card and the AI4Bharat GitHub page), CTC and RNNT decoding, language given.
- Packages: transformers 5.18.0, onnxruntime-gpu 1.31.0, onnx 1.20.1, torchaudio 2.11.0+cu130 (Colab preinstalled). These are much newer than the versions on the model card.
- Hardware: Tesla T4 (15 GB), Google Colab, 2026-10-11. The first call per decoding mode is a warm-up and is not timed.
- The repository is gated on Hugging Face (account and accepted conditions) and ships its own loader code (`trust_remote_code`), which runs inside the Colab session. The token lives in Colab Secrets and should be deleted afterwards.
- Workaround: onnxruntime 1.31 rejects weight files that resolve outside the model folder, and the Hugging Face cache uses symlinks. The script downloads the model into a normal local folder of real files and points the model's loader at it.

Limits specific to this comparison:

- The RTF column in this 20-clip table is not comparable across systems, and the device was not checked in this run. A later run (next section) measured that IndicConformer used the GPU. Read accuracy first.
- 20 clips per language, one run, no confidence intervals.
- Whether either model saw FLEURS-like data in training has not been checked.
- Two open models only. Nothing here compares against a commercial service.
- The remote loader code has not been independently audited, and the model is gated, so it is not eligible as a default model (see `packages/ragleap-voice/models.toml`).

## Five Indian languages, 60 clips each (Whisper large-v3 vs IndicConformer-600M)

Third measurement: the first 60 FLEURS test clips for each of Hindi, Tamil, Telugu, Malayalam and Kannada (300 clips), same normalizer and same packages as above. Intervals are 95% bootstrap intervals over clips (2000 resamples). Whisper was run with temperature 0, beam 5 and no context carry-over; IndicConformer with CTC and RNNT decoding.

| Language | System | WER (95% interval) | CER (95% interval) | RTF |
|---|---|---|---|---|
| Hindi | Whisper large-v3 | 26.8% [24.3, 29.4] | 8.6% [7.4, 10.0] | 0.219 |
| Hindi | IndicConformer CTC | 10.6% [8.4, 13.0] | 3.5% [2.4, 4.9] | 0.010 |
| Hindi | IndicConformer RNNT | 10.2% [7.9, 12.9] | 3.6% [2.4, 5.2] | 0.029 |
| Tamil | Whisper large-v3 | 49.3% [44.5, 54.3] | 13.1% [10.7, 16.4] | 0.196 |
| Tamil | IndicConformer CTC | 29.0% [24.1, 34.5] | 11.2% [7.6, 16.3] | 0.007 |
| Tamil | IndicConformer RNNT | 27.5% [23.0, 32.6] | 10.9% [7.2, 16.1] | 0.029 |
| Telugu | Whisper large-v3 | 73.9% [69.7, 77.9] | 33.1% [27.3, 38.6] | 0.351 |
| Telugu | IndicConformer CTC | 21.8% [16.8, 27.3] | 7.1% [4.6, 10.8] | 0.009 |
| Telugu | IndicConformer RNNT | 21.4% [16.4, 26.9] | 7.0% [4.5, 10.6] | 0.032 |
| Malayalam | Whisper large-v3 | 111.4% [107.4, 115.9] | 88.9% [86.1, 91.3] | 0.267 |
| Malayalam | IndicConformer CTC | 21.8% [18.0, 25.8] | 5.9% [4.2, 7.8] | 0.007 |
| Malayalam | IndicConformer RNNT | 20.8% [17.2, 24.5] | 5.7% [4.1, 7.6] | 0.029 |
| Kannada | Whisper large-v3 | 67.2% [62.9, 71.2] | 24.9% [20.3, 29.8] | 0.289 |
| Kannada | IndicConformer CTC | 16.1% [12.9, 19.4] | 4.0% [2.6, 5.7] | 0.007 |
| Kannada | IndicConformer RNNT | 16.5% [13.0, 19.8] | 3.9% [2.4, 5.6] | 0.029 |

Paired difference in word error rate (Whisper minus IndicConformer RNNT, 95% bootstrap interval over clips): Hindi [14.2, 18.9], Tamil [16.8, 26.8], Telugu [45.1, 58.8], Malayalam [84.9, 96.4], Kannada [46.1, 54.9] percentage points. No interval includes zero.

Whisper with library-default decoding (temperature fallback and context carry-over on), run on Telugu, Malayalam and Kannada to check that the settings above did not handicap it:

| Language | WER | CER | RTF |
|---|---|---|---|
| Telugu | 73.9% | 33.1% | 0.347 |
| Malayalam | 113.6% | 89.5% | 0.357 |
| Kannada | 67.5% | 25.3% | 0.296 |

What it shows:

- On this sample IndicConformer-600M is clearly more accurate than Whisper large-v3 in all five languages. The Whisper decoding settings made no material difference (within 2.2 points of WER).
- Whisper's Malayalam word error rate above 100% means it produced many wrong or extra words. In one inspected clip it wrote Telugu-script text for Malayalam audio even though the language was set to Malayalam. That is one clip, not a general finding.
- CTC and RNNT decoding are within about two points of each other in every language. In Tamil, character error rates overlap between all three systems.

Device and speed: GPU memory grew by 3,564 MiB when IndicConformer loaded and ran its first call, so it used the GPU (a memory heuristic, not a profiler). Both systems ran on the same Tesla T4, but the model sizes (about 1.5B parameters against about 0.6B) and runtimes (CTranslate2 fp16 against ONNX Runtime) differ, so RTF is indicative, not a like-for-like speed comparison.

Limits specific to this measurement:

- The 60 clips per language are the first consecutive clips of the test split, so speakers and sentences repeat. The bootstrap treats clips as independent, so the intervals are probably too narrow.
- FLEURS is read speech. Phone calls, noise and spontaneous speech may behave very differently. Whether either model saw FLEURS-like data in training has not been checked.
- Two open models only. Nothing here compares against a commercial service.
- IndicConformer has no English. The model is gated and loads its own repo code, which has not been independently audited, so it is not eligible as a default model.

## TTS round trip: Indic Parler-TTS, Kokoro-82M, Chatterbox (first measurement)

Intelligibility proxy for text-to-speech: 20 sentences per language (text of FLEURS test sentences, CC-BY-4.0; sentences with digits, and Latin letters inside Indic text, were skipped) are synthesized, transcribed back by a fixed recogniser, and compared with the original text using the same normalizer as above. The recogniser is IndicConformer-600M (RNNT) for Indian languages and Whisper large-v3 (beam 5) for English. Speed is generation time divided by audio length on a Tesla T4 (below 1 means faster than real time). Built-in voices only, no cloning, no emotion prompts.

| System | Language | Round-trip WER (95% interval) | CER | Speed |
|---|---|---|---|---|
| Kokoro-82M | Hindi | 7.9% [4.3, 11.8] | 1.5% | 0.03 |
| Kokoro-82M | English | 1.8% [0.5, 3.4] | 0.8% | 0.03 |
| Indic Parler-TTS | Hindi | 7.7% [4.5, 10.9] | 2.0% | 2.15 |
| Indic Parler-TTS | Tamil | 16.1% [10.4, 22.2] | 4.7% | 2.13 |
| Indic Parler-TTS | Telugu | 11.9% [6.5, 17.4] | 1.1% | 2.14 |
| Indic Parler-TTS | Malayalam | 22.0% [15.3, 28.7] | 4.2% | 2.14 |
| Indic Parler-TTS | Kannada | 16.7% [11.6, 22.0] | 3.1% | 2.14 |
| Indic Parler-TTS | English | 5.8% [2.8, 10.1] | 3.5% | 2.14 |
| Chatterbox Multilingual | Hindi | 18.9% [8.3, 36.6] | 11.4% | 1.21 |
| Chatterbox Multilingual | English | 3.2% [0.8, 6.5] | 2.0% | 1.26 |

Method:

- Date 2026-10-12, Tesla T4 (15 GB), Google Colab. Model revisions (prefixes): `hexgrad/Kokoro-82M` f3ff357179, `ai4bharat/indic-parler-tts` 660bc53c64, `ResembleAI/chatterbox` 5bb1f6ee58. Kokoro 0.9.4 with misaki 0.9.4, voices `hf_alpha` (Hindi) and `af_heart` (English). Chatterbox: chatterbox-tts 0.1.7, torch 2.6.0+cu124. Parler: parler-tts at GitHub commit d108732cd5, transformers 4.46.1. Kokoro and Parler ran with torch 2.14.1+cu130. Each system ran in its own Python 3.11 environment.
- Parler used one fixed voice description (female speaker, slightly expressive, moderate speed and pitch, very clear recording) for every sentence.
- Parler's repository is gated on Hugging Face (account and accepted conditions). Its numbers come from the printed output of the run; raw per-clip files for the TTS runs are not committed.

What it shows:

- All three systems produced intelligible speech in the languages tested. Kokoro was far faster than real time on the T4; Parler and Chatterbox were slower than real time at default settings with no optimisation.
- Chatterbox's Hindi point estimate is worse than Kokoro's and Parler's, but its interval is wide and overlaps Kokoro's, so 20 sentences do not establish a difference.
- Only Parler covers all five Indian languages tested here.

Limits specific to this measurement:

- Round-trip error includes the recogniser's own errors. The Indian-language recogniser and Parler both come from AI4Bharat and may share training data, which could flatter Parler. Compare systems with each other, not against any absolute number.
- The sentences are easier than real-world text (no digits, no Latin letters in Indic text).
- 20 sentences per language, one run, one voice per system. Differences between languages are partly noise.
- Naturalness, emotion quality and voice similarity were not measured. They need human listeners (naturalness, emotion) and consented reference recordings (voice similarity).
- Speeds are not like-for-like: different libraries, different torch versions (Chatterbox) and no optimisation (Parler is slower than real time at defaults; its documentation describes faster inference modes that were not tested). GPU only; CPU speed and time to first audio were not measured.
- Kokoro's Hindi path uses the GPLv3 espeak-ng package according to its model card; it was installed from the OS package manager for this measurement only.
- `colab_tts_roundtrip.py` was assembled from three cells that were each run successfully. The combined script has not been run end to end as one piece.

## Whisper large-v3 on 12 languages from several regions (pilot)

First non-Indian measurement for the language coverage matrix. Scripts: `colab_pilot12.py` (measurement) and `colab_pilot12_analysis.py` (intervals and diagnostics, same Colab session).

| Language (FLEURS config) | WER (95% interval) | CER (95% interval) |
|---|---|---|
| Spanish (es_419) | 3.0% (1.8 to 4.5) | 1.1% (0.5 to 1.9) |
| Portuguese (pt_br) | 4.2% (2.9 to 5.7) | 1.4% (0.9 to 2.1) |
| Russian (ru_ru) | 4.9% (3.5 to 6.8) | 1.2% (0.7 to 1.7) |
| Turkish (tr_tr) | 5.1% (3.5 to 6.9) | 1.3% (0.8 to 2.0) |
| Indonesian (id_id) | 5.6% (3.8 to 7.5) | 1.8% (1.1 to 2.5) |
| French (fr_fr) | 6.1% (4.4 to 8.1) | 2.1% (1.4 to 3.0) |
| Vietnamese (vi_vn) | 8.8% (6.8 to 10.7) | 3.9% (2.9 to 4.9) |
| Arabic (ar_eg) | 14.0% (10.6 to 17.6) | 3.5% (2.6 to 4.6) |
| Swahili (sw_ke) | 33.5% (29.7 to 37.2) | 7.7% (6.5 to 9.1) |
| Japanese (ja_jp) | 70.6% (61.5 to 79.0): not meaningful, no word spaces | 6.5% (4.9 to 8.3) |
| Yoruba (yo_ng) | 103.8% (99.4 to 108.9) | 48.0% (45.3 to 50.6) |
| Amharic (am_et) | 162.1% (147.0 to 178.7) | 110.1% (103.8 to 117.4) |

Setup: 60 clips per language (first 60 of the FLEURS `test` split, CC-BY-4.0), run on 2026-10-18 on a Tesla T4. Model: `Systran/faster-whisper-large-v3` at revision `edaa852ec7e145841d8ffdb056a99866b5f0a478` (MIT), pulled with `ragleap-voice models pull` (hash-checked, CTranslate2-tagged lock) and loaded from that folder; faster-whisper 1.1.1, fp16, beam 5, temperature 0, no conditioning on previous text, language given. Harness on main at `d441b746c8`; normalizer version `1` (no number step). Intervals: 2000 bootstrap resamples of clips, seed 1234.

Findings:
- Spanish, Portuguese, Russian, Turkish, Indonesian and French are between 3% and 6% WER. Their intervals overlap, so their order is not reliable.
- Swahili (33.5%) is a real recognition problem.
- Japanese: use CER. WER is not meaningful without word spaces.
- Yoruba: scoring again with diacritics removed from both sides gives WER 87.2% and CER 26.7%. A large part of the character error is missing tone marks and dot-below letters, but the output is still not usable text. The stripped figures still include Whisper's own romanisation (for example "ishe" for "ise"), so they do not separate spelling convention from mishearing.
- Amharic: with our settings 36 of 60 outputs collapsed into a repeated syllable or word. Library defaults gave WER 114.1% and CER 104.7%, and a repetition guard gave WER 102.5% and CER 101.5%. In none of the three settings did the outputs resemble the reference. These variants were diagnostics run on the test set, not tuning, and the pilot numbers above stay the reported result. We did not find a usable Whisper large-v3 setting for Amharic; this is not a statement about every possible setting.

Limits (read before quoting any number):
- Read speech only. No telephone or noisy audio, no accents or code-switching.
- Consecutive clips: speakers and sentences repeat, so the intervals are probably too narrow.
- Arabic uses the Egyptian dialect configuration, while Whisper's `ar` is standard Arabic, so part of that error may be dialect mismatch. Spanish is the Latin American configuration.
- One system. This shows where Whisper large-v3 is weak, not which open model is best for those languages.
- Numbers and digits are not unified (normalizer version 1), which slightly inflates WER where references contain digits.
- The per-clip raw CSV is not committed yet; the tables come from the printed run output.

## Fine-tuned candidates for Swahili, Yoruba and Amharic (first look, possibly contaminated)

Whisper large-v3 was weak or failed in these three languages (pilot above). Two open fine-tunes of Whisper large-v3 were measured on the same 60 FLEURS `test` clips per language. Scripts: `colab_african_candidates_download.py`, `colab_african_candidates_measure.py`, `colab_african_candidates_unicode_check.py`.

| Language | Whisper large-v3 (pilot) | NaijaVox-2.0 | SunflowerASR |
|---|---|---|---|
| Swahili (sw_ke), WER (95% interval) | 33.5% (29.7 to 37.2) | not a candidate | 8.9% (7.0 to 11.0) |
| Swahili, CER | 7.7% | | 2.4% (1.8 to 3.0) |
| Yoruba (yo_ng), WER | 103.8% (99.4 to 108.9) | 46.5% (40.5 to 53.1) | 41.8% (35.6 to 48.6) |
| Yoruba, CER | 48.0% | 16.6% (13.9 to 19.5) | 15.1% (12.5 to 17.9) |
| Yoruba, diacritics removed from both sides, WER / CER | 87.2% / 26.7% | 24.1% / 7.2% | 20.7% / 5.8% |
| Amharic (am_et), WER | 162.1% (147.0 to 178.7) | not a candidate | 28.8% (24.6 to 33.2) |
| Amharic, CER | 110.1% | | 14.0% (10.1 to 18.5) |

Models (downloaded with `huggingface_hub` at these revisions; **not** hash-checked by ragleap-voice, no lock exists yet):
- `Axiveri/NaijaVox-2.0` at `22b94de94e59419cc9d6e25e6eaeceb1f1c42d00`: Apache-2.0 per its card, Yoruba, Hausa, Igbo, Pidgin and Nigerian English, single safetensors file, no code files.
- `Sunbird/asr-whisper-51-african-languages` (the Hub now reports `Sunbird/SunflowerASR-51-african-languages`) at `4583c9dd251c422a4325fb0a0a509a3828097be2`: Apache-2.0 per its card, 51 African languages, gated (access mode auto), single safetensors file, no code files. Language tokens come from its card: `swa` 50318, `yor` 50325, `amh` 50334.

Read these first:
- **Possibly contaminated.** Both cards list google/fleurs among their training data (NaijaVox: the yo_ng, ha_ng and ig_ng configurations; SunflowerASR: FLEURS among its sources). Which splits they used is not stated. We scored on FLEURS test clips, so these numbers show that a large gap exists, not how well the models do on unseen audio. A clean test set is still needed (issue #758).
- **Licences are not cleared.** Apache-2.0 is the licence of the weights as stated on the card. Both cards say the training datasets keep their own licences, and we have not reviewed them. Neither model is in `models.toml`, and neither belongs in the default path until a person has reviewed the training-data licences.
- **Not like-for-like with the Whisper pilot.** Different engine (`transformers` instead of faster-whisper), greedy decoding instead of beam 5, fp16, batch of 8, `max_new_tokens` 220. Decoding settings were fixed before scoring. One difference from the run: the committed measure script calls `model.train(False)` where the run used the equivalent evaluation-mode call, because the CI scanner flags that call name as dynamic code; nothing else was changed. The `transformers` version was not recorded in this run.
- **Unexplained gap to a model card.** The NaijaVox card reports 22.3% WER on 50 FLEURS Yoruba test clips ("strict WER, no normalization"). We measure 46.5% with diacritics counted and 24.1% without them. We checked Unicode composition (references 0/60 and outputs 3/60 not in NFC; NFC on both sides changed WER by under 0.1 points), so that is not the cause. We do not know which clips, scorer or decoding the card used.
- **Yoruba.** About half of the word error comes from tone marks and dot-below letters. Both models produce real Yoruba text, which Whisper large-v3 did not.
- **Amharic.** SunflowerASR output resembles the reference where Whisper large-v3 produced repeated syllables.
- Same limits as the other sections: read speech only, consecutive clips (speakers and sentences repeat, so the intervals are probably too narrow), 60 clips, one GPU (Tesla T4), normalizer version 1 (no number step), per-clip raw results not committed.

## Noise, telephone audio and two denoisers in front of Whisper large-v3 (pilot)

Question: does an open denoiser in front of Whisper large-v3 lower the word error rate under noise and over a telephone-style channel? Scripts, to be run in this order in one Colab session: `colab_noise_pilot.py` (first run), `colab_noise_pilot_diagnostics.py`, `colab_noise_pilot_levelfixed.py` (the run reported below). The scripts are the cells as they ran; the diagnostics file joins two cells and has not been run as one piece.

Setup: FLEURS `test` clips (CC-BY-4.0) for English (en_us), Spanish (es_419) and Russian (ru_ru), the first 30 clips per language (a subset of the 12-language pilot clips), Whisper large-v3 as in the pilot above (pulled with `ragleap-voice models pull`, beam 5, language given), run on 2026-10-19 on a Tesla T4. Five conditions: clean; white noise at 10 dB SNR; babble at 10 dB (four other FLEURS speakers of the same language, clips 30 to 59, mixed at equal level); babble at 5 dB; telephone (babble at 10 dB, then 8 kHz mu-law, then back to 16 kHz). Every clip is scaled to -23 dBFS RMS (peak limit 0.95) before the denoisers, so all three variants see identical levels. Denoisers, default settings, no tuning: RNNoise v0.2 (commit `904a876dce1f9ab8860c0a5000ed151f9f6eef58`, model version `0b50c45`, built from source) and DeepFilterNet 0.5.6 (torch 2.0.1 on CPU, model DeepFilterNet3). Word error rate with a 95% bootstrap interval for no denoiser; for each denoiser the paired difference to no denoiser on the same clips (denoiser minus none, percentage points, 2000 resamples of clips, seed 1234). Harness on main at `ef018740b1`, normalizer version `1`.

| Language | Condition | No denoiser WER (interval) | RNNoise WER, difference | DeepFilterNet3 WER, difference |
|---|---|---|---|---|
| English | clean | 5.5% (4.0 to 7.4) | 6.7%, +1.2 [-0.8, +3.8] | 6.3%, +0.7 [-0.5, +2.2] |
| English | white noise 10 dB | 7.5% (5.2 to 10.3) | 13.0%, +5.5 [+2.6, +9.4] | 9.3%, +1.8 [-0.9, +4.4] |
| English | babble 10 dB | 7.5% (5.3 to 10.1) | 12.9%, +5.4 [+1.6, +10.5] | 11.7%, +4.2 [+1.1, +7.8] |
| English | babble 5 dB | 14.1% (8.2 to 22.3) | 23.7%, +9.6 [-1.0, +21.8] | 34.5%, +20.4 [+1.6, +45.0] |
| English | telephone | 9.7% (7.2 to 12.4) | 16.3%, +6.6 [+3.2, +10.7] | 18.6%, +8.8 [+4.3, +14.6] |
| Spanish | clean | 2.8% (0.9 to 5.3) | 2.5%, -0.3 [-1.2, +0.4] | 2.5%, -0.3 [-1.2, +0.4] |
| Spanish | white noise 10 dB | 5.2% (2.9 to 8.5) | 6.0%, +0.8 [-0.3, +1.8] | 7.7%, +2.5 [+0.9, +4.5] |
| Spanish | babble 10 dB | 3.9% (1.3 to 7.9) | 6.5%, +2.7 [+1.0, +4.8] | 5.7%, +1.9 [+0.5, +3.4] |
| Spanish | babble 5 dB | 9.5% (4.7 to 16.9) | 21.8%, +12.3 [+7.0, +18.3] | 19.6%, +10.1 [+6.2, +15.4] |
| Spanish | telephone | 4.9% (2.1 to 9.1) | 8.3%, +3.3 [+1.1, +6.1] | 9.3%, +4.4 [+2.2, +7.0] |
| Russian | clean | 5.2% (2.9 to 8.1) | 13.5%, +8.3 [+0.3, +22.3] | 5.5%, +0.3 [+0.0, +0.8] |
| Russian | white noise 10 dB | 10.4% (6.7 to 14.9) | 17.4%, +7.0 [+3.8, +11.0] | 15.6%, +5.2 [+2.6, +8.6] |
| Russian | babble 10 dB | 9.3% (5.8 to 13.2) | 12.3%, +3.1 [+0.7, +5.5] | 15.1%, +5.8 [+1.8, +11.1] |
| Russian | babble 5 dB | 18.2% (11.9 to 26.2) | 26.1%, +8.0 [+2.5, +13.7] | 46.6%, +28.4 [+11.4, +55.3] |
| Russian | telephone | 12.0% (8.1 to 16.4) | 17.9%, +5.8 [+3.0, +8.8] | 35.9%, +23.9 [+16.4, +32.5] |

What it shows:

- In none of the 30 comparisons (2 denoisers, 3 languages, 5 conditions) did a denoiser lower the word error rate: no interval lies below zero. In 22 of 30 the denoiser made it worse, with the interval entirely above zero; the other 8 show no clear difference. The telephone condition was worse with both denoisers in all three languages.
- This is a result for these conditions only: synthetic white noise and babble at 10 and 5 dB, default denoiser settings, one recogniser. It does not show that denoisers never help. Lower SNRs and real-world stationary noise were not tested. Babble is speech, which these denoisers are not built to separate.
- Level matters for denoisers. More than half of the English clips in this subset are recorded very quietly (17 of 30 at or below -55.9 dBFS RMS; one more, at -50.7 dBFS, came out at 0.11 of its input level). In the first run, DeepFilterNet3 returned silence for those clips and English clean WER rose to 65.2% (RNNoise did not). That first-run DeepFilterNet figure is an artefact and is not reported above. After scaling every clip to -23 dBFS, no output was silent (0 of 900) and English clean WER was 6.3%. Batch versus single-file calls made no difference.
- Whisper large-v3 wrote "Thank you." for silent audio. A telephone pipeline needs a voice-activity or silence gate in front of the recogniser.
- The level fix also moved some no-denoiser baselines (Russian white noise 17.5% to 10.4%, Russian babble 5 dB 24.5% to 18.2%, English babble 5 dB 12.0% to 14.1%). The intervals overlap, so with 30 clips the absolute numbers are loose; read the comparisons within one table.

Denoiser provenance and licences (both are candidates only, neither is in `models.toml`): RNNoise code is BSD-3-Clause according to GitHub and its licence file; its model data is fetched by a script that checks a SHA-256, and the licence of that data was not checked. DeepFilterNet code is MIT or Apache-2.0 according to its README; the pretrained model was fetched to the local cache at first use (DeepFilterNet3 checkpoint SHA-256 `23b92884f63ccf54bb026014604625ab231657b6480df65db4095c4c171e6003`) and no separate weights licence is stated. Neither was hash-checked by ragleap-voice.

Limits (read before quoting any number):

- One recogniser (Whisper large-v3), three languages, 30 clips per language: intervals are wide, and the Russian clean RNNoise cell shows how a few clips move a result.
- Read speech with synthetic noise. Babble speakers come from the same corpus and may repeat the test speakers. The telephone path is a simulation (8 kHz mu-law), not a real call.
- Default denoiser settings, no tuning. DeepFilterNet is designed for 48 kHz audio and received 16 kHz speech upsampled to 48 kHz.
- Not measured in this section: other engines under noise (two are measured without a denoiser in the next section), spontaneous speech, real recordings, latency of the denoisers inside a call.
- The per-clip results are in the Colab session only (`noise_pilot_levelfixed_clips.csv`) and are not committed.

## Two engines under noise without a denoiser: SunflowerASR and IndicConformer (first run)

Question: how much does each engine lose under noise and over a simulated telephone channel, with no denoiser in front? Scripts: `colab_noise_engines_download.py` and `colab_noise_engines_measure.py`, run in that order on 2026-10-21 on a Tesla T4. Each file has a header docstring that was added after the run; the code is otherwise as run.

Setup: the same clips, five conditions, level fix and scoring as the noise pilot above: FLEURS `test` (CC-BY-4.0), the first 30 clips per language (Swahili sw_ke, Amharic am_et, Hindi hi_in, Tamil ta_in), babble from clips 30 to 59 of the same language, every clip scaled to -23 dBFS RMS, normalizer version `1`, 95% bootstrap intervals (2000 resamples of clips, seed 1234). Differences are condition minus clean on the same clips. SunflowerASR (revision `4583c9dd251c422a4325fb0a0a509a3828097be2`, Apache-2.0 per its card) was pulled with `ragleap-voice models pull` and verified before use, and loaded from that folder with `local_files_only=True` and no token in the environment; `HF_HUB_OFFLINE` was not set. Greedy decoding with the language token forced from the card (`swa` 50318, `amh` 50334), fp16, batch of 8, `max_new_tokens` 220. IndicConformer-600M (revision `2a77d305038dfc502fd9464e7d028b90381976cb`, MIT per its card) was downloaded at the pinned revision (not locked), RNNT decoding, 25 s chunks; it runs its own repository code, which ran in a session with no token. `transformers` 5.18.0, `torch` 2.11.0+cu130, harness on main at `dd1849d97f`.

| Engine, language | Clean WER (interval) | White noise 10 dB | Babble 10 dB | Babble 5 dB | Telephone |
|---|---|---|---|---|---|
| SunflowerASR, Swahili | 8.9% (6.9 to 11.2) | 20.7%, +11.8 [+6.6, +18.2] | 21.6%, +12.6 [+5.6, +21.6] | 49.3%, +40.4 [+24.9, +57.9] | 28.5%, +19.6 [+9.4, +31.5] |
| SunflowerASR, Amharic | 32.9% (25.9 to 39.8) | 39.0%, +6.1 [+3.9, +8.3] | 42.5%, +9.6 [+3.9, +16.5] | 48.9%, +16.0 [+9.8, +23.6] | 47.8%, +14.9 [+8.5, +22.4] |
| IndicConformer, Hindi | 10.0% (6.9 to 13.8) | 13.3%, +3.3 [+1.4, +5.4] | 15.8%, +5.8 [+1.3, +13.6] | 33.3%, +23.3 [+12.9, +36.8] | 16.3%, +6.3 [+3.1, +10.1] |
| IndicConformer, Tamil | 25.6% (19.4 to 33.2) | 28.1%, +2.5 [-3.3, +7.1] | 29.6%, +4.0 [-4.3, +10.9] | 46.9%, +21.4 [+11.2, +33.6] | 35.1%, +9.5 [+2.9, +17.1] |

What it shows:

- No engine was immune. In all four engine and language pairs, babble at 5 dB and the telephone path raised the word error rate with the interval entirely above zero (+16 to +40 points at 5 dB, +6 to +20 points over the telephone path). Two Tamil cells (white noise and babble at 10 dB) are inconclusive: their intervals include zero.
- The engines fail differently. IndicConformer returned an empty output for 7 of its 300 decodes (4 in Hindi and 3 in Tamil, all under noise). SunflowerASR returned no empty output in 300. Neither produced any of the filler phrases the script looks for ("thank you", "thanks for watching", "you", "."), a short list that does not detect other kinds of invented text.
- The engines ran on different languages, so these numbers do not rank them. Each engine is compared with its own clean baseline only. The Whisper results above are not comparable: different languages, a different library and different decoding.
- The SunflowerASR model card says it was trained with added noise and 8 kHz augmentation. These results neither confirm nor refute that: Swahili still lost 40 points at 5 dB, from the lowest clean baseline in the table.

Limits (read before quoting any number):

- 30 clips per language: wide intervals. FLEURS is read speech with synthetic noise; babble speakers come from the same corpus and may repeat the test speakers; the telephone path is a simulation (8 kHz mu-law), not a call. Consecutive clips repeat speakers and sentences, so the intervals are probably too narrow.
- SunflowerASR's card lists FLEURS among its training data (see the section on fine-tuned candidates): possibly contaminated, especially the clean baseline. Whether IndicConformer saw FLEURS-like data has not been checked.
- No denoiser was tested with these engines. The finding that two denoisers did not help Whisper large-v3 says nothing about them.
- Default clean figures here (30 clips, level-fixed) differ slightly from the earlier 60-clip tables (for example SunflowerASR Amharic 32.9% here against 28.8% there): different clip counts and a level fix.
- The per-clip results are in the Colab session only (`noise_engines_clips.csv`) and are not committed.

## Energy gate: false drops and invented text (first measurement)

Script: `colab_gate_benchmark.py` (Colab T4, no token; ragleap-voice installed from main, faster-whisper 1.2.1, Whisper large-v3 revision `edaa852ec7e1`). Run on 2026-10-27, one run, one seed. The file was assembled from the notebook cell afterwards, so it may differ from it in whitespace or comments.

**A. Real speech, as recorded (no level fix), 30 FLEURS test clips per language: how many pass the gate** (Wilson 95% interval).

| Rule (floor, 30 dB relative) | en | es | ru |
|---|---|---|---|
| default, -46.8 dBFS | 14/30 (30.2-63.9%) | 30/30 | 29/30 |
| -60 dBFS | 30/30 | 30/30 | 30/30 |
| -70 dBFS | 30/30 | 30/30 | 30/30 |
| -80 dBFS | 30/30 | 30/30 | 30/30 |

English clip RMS: min -66.6, median -57.4, max -19.7 dBFS; 16 of 30 at or below -55.9. Spanish median -25.5, Russian median -39.2.

**B. Whisper large-v3 (language en, CUDA) on synthetic non-speech, 10 s clips.** Digital silence: 5 clips. White noise: 30 clips per level.

| Input | Clips with text | Text | Stopped by default / -60 / -70 / -80 |
|---|---|---|---|
| silence | 5/5 | "Thank you." | 5 / 5 / 5 / 5 |
| white -70 dBFS | 30/30 | "Thank you." | 30 / 30 / 0 / 0 |
| white -60 dBFS | 30/30 | "Thank you." | 30 / 0 / 0 / 0 |
| white -50 dBFS | 30/30 | "Thank you." | 30 / 0 / 0 / 0 |
| white -40 dBFS | 30/30 | "Thank you." | 0 / 0 / 0 / 0 |
| white -20 dBFS | 30/30 | "Thank you." (28), "Thanks for watching!" (2) | 0 / 0 / 0 / 0 |

- Whisper wrote a filler phrase for every silent or noisy clip, with the engine's own decoding settings (not the pilot's).
- Levels of quiet real speech and of noise overlap. The default floor drops 16 of 30 English clips; a floor low enough to keep them (-60) lets noise at -60 dBFS and above through to the recogniser. A level gate cannot separate them, so the gate stays off by default and no protection is claimed beyond digital silence and very quiet noise.
- Limits: synthetic white noise only; part B in English only; small samples; one seed; no real room or telephone noise; three languages in part A.

## StreamGate on noise (first measurement, gate only)

Script: `colab_gate_noise_benchmark.py` (Colab, CPU is enough, no token; ragleap-voice 0.3.2). Run on 2026-11-02, one run, seed 1. 30 public FLEURS English test clips for speech; white noise, 50 Hz hum and digital silence are generated in the script. No speech model was run: this measures the gate only. A trigger is a `speech_start` event; intervals are Wilson 95%.

**Noise only (a trigger is a false accept; lower is better):**

| input | default floor -46.8 | floor -60 |
|---|---|---|
| digital silence | 0/30 [0.00-0.11] | 0/30 [0.00-0.11] |
| white noise -60 dBFS | 0/30 [0.00-0.11] | 29/30 [0.83-0.99] |
| white noise -50 dBFS | 0/30 [0.00-0.11] | 30/30 [0.89-1.00] |
| white noise -40, -30, -20 dBFS | 30/30 [0.89-1.00] | 30/30 [0.89-1.00] |
| 50 Hz hum -40, -30 dBFS | 30/30 [0.89-1.00] | 30/30 [0.89-1.00] |

**Speech plus white noise (a trigger is a detection):** clean speech and SNR 20, 10 and 0 dB gave the same result, 13/30 [0.27-0.61] at the default floor and 29/30 [0.83-0.99] at floor -60. Noise alone also triggers, so this does not show that speech is separated from noise: the outcome follows the level of the speech clip itself.

- The default floor drops 17 of 30 quiet FLEURS English clips; floor -60 keeps 29 of 30 but passes noise at -60 dBFS and louder.
- Steady noise at -40 dBFS and louder passes with either floor. A level gate cannot separate it from speech, so `gate` stays off by default and no protection is claimed beyond digital silence and very quiet noise.
- `StreamGate` needs 5 consecutive speech-like frames to start; the batch gate measured above counts speech-like frames in total. The 13/30 and 14/30 pass counts are close but are not the same test.
- Not measured: real room or phone noise, music, babble, other languages, 8 kHz telephone audio, a model-based detector (its licence is unverified).

## Silero VAD (ONNX) on the same noise inputs (first measurement)

Script: `colab_silero_vad_benchmark.py` (Colab, CPU, no token). Run on 2026-11-03, one run, seed 1, same 30 FLEURS English clips, noise generator and seed as the StreamGate benchmark above. Model file: `silero_vad_16k_op15.onnx` from `snakers4/silero-vad` at commit `066409f3d9db45a30d21dfcbf9147826ffdd7052`, 1,289,603 bytes, SHA-256 `7ed98ddbad84ccac4cd0aeb3099049280713df825c610a8ed34543318f1b2c49`. Read with the `onnx` package: opset 15, standard `ai.onnx` operators only, no custom domains, no external data. A trigger is 3 consecutive 32 ms frames with probability at least 0.5; both numbers are unmeasured starting points. Intervals are Wilson 95%.

| input | StreamGate default floor / floor -60 | Silero ONNX |
|---|---|---|
| digital silence | 0/30 / 0/30 | 0/30 [0.00-0.11] |
| white noise -60 to -20 dBFS | 0/30 up to 30/30 (see above) | 0/30 at every level |
| 50 Hz hum -40, -30 dBFS | 30/30 | 0/30 |
| clean speech (detected) | 13/30 / 29/30 | 30/30 [0.89-1.00] |
| speech + noise, SNR 20, 10, 0 dB (detected) | 13/30 / 29/30 | 30/30 |

The sanity check passed first (maximum probability 1.00 on speech, 0.01 on silence).

- On these inputs Silero separates speech from steady synthetic noise, which a level gate cannot do.
- Limits: the noise is synthetic (white noise and a 50 Hz hum), which is the easy case; the speech is clean read English; 16 kHz only, 8 kHz telephone audio is not tested; one run. Speech plus noise at 30/30 shows speech is still detected, not that it is separated at every SNR.
- Not established: training-data provenance; the weights' licence beyond the repository's MIT licence and a README statement that it is published under a permissive licence (MIT); behaviour on real room noise, music, babble or phone-line artefacts.
- Silero is not in `models.toml`, not an extra and not a default. Adding it would need a manifest entry and lock file, which need the maintainer's review.

## Silero VAD (ONNX) vs StreamGate: more synthetic noise and a 16k to 8k to 16k path (second measurement)

Script: `colab_silero_vad_benchmark2.py` (Colab, CPU, no token). Run on 2026-11-03, one run, seed 1, same pinned Silero file as above, 30 trials per row, Wilson 95% intervals.

| input (5 s, trigger = false accept) | gate default | gate floor -60 | Silero |
|---|---|---|---|
| pink -50 dBFS | 10/30 | 30/30 | 0/30 |
| band-limited 300-3400 Hz, clipped white -50 dBFS | 0/30 | 30/30 | 0/30 |
| pink, band-limited, clipped white at -35 and -20 dBFS | 30/30 | 30/30 | 0/30 |
| same inputs after 16k to 8k to 16k (pink, band-limited at -35, -20) | 30/30 | 30/30 | 0/30 |
| clean speech, detected: 16 kHz and after the 8 kHz path | 13/30 | 29/30 | 30/30 |
| babble of 3 speakers at -30 dBFS (speech, so a trigger is correct) | 30/30 | 30/30 | 30/30 |

- Silero stayed at 0/30 on every non-speech noise and kept all clean speech after the resample path; the level gate did not.
- The babble row is one fixed input repeated 30 times, not 30 independent inputs. It also shows Silero cannot tell the target speaker from background talkers.
- Limits: all noise is synthetic; the 8 kHz path is a resample with `Down2Stream` and `Up2Stream`, not a G.711 codec or a real phone line; thresholds (probability 0.5, 3 frames) are unmeasured; one run.
- Still not established: training-data provenance, the weights' licence beyond the repository's MIT licence and a README statement, behaviour on real recorded noise, and onnxruntime behaviour on a malformed file.
- Silero is still not in `models.toml`, not an extra and not a default.

## Audio helpers in pure Python: speed on 20 ms frames (prototype)

Question: can the standard-library-only core do the audio conversions the phone contract needs (mu-law, level, gain, 8 to 16 kHz and back) in real time, now that `audioop` is gone from Python 3.13? Script: `audio_helpers_speed.py` (a prototype for `docs/design/audio-helpers.md`, not package code). Run on 2026-10-22 on the project VPS: Python 3.10.12, x86_64, 1 CPU, load average 0.14, `nice -n 19`, best of 3 rounds of 150 repetitions per helper. `audioop` (present on 3.10) is timed alongside for reference.

| One 20 ms frame | Pure Python | audioop |
|---|---|---|
| mu-law decode, 160 samples | 16.0 us | 0.2 us |
| mu-law encode, 160 samples | 11.9 us | 1.3 us |
| RMS, 160 samples | 8.1 us | 0.6 us |
| gain, 160 samples | 67.5 us | 1.1 us |
| resample 16 to 8 kHz, 320 samples in | 325.1 us | 4.9 us |
| resample 8 to 16 kHz, 160 samples in | 860.8 us | 4.0 us |

Chains: inbound (decode, resample up, RMS, gain) 952 us per frame; outbound (resample down, encode) 337 us per frame. One call in both directions: 1289 us of CPU per 20000 us of audio, 6.4% of one core, so at most about 15 simultaneous calls on one core if nothing else ran.

What it shows:

- A pure-Python default is workable for a few simultaneous calls and not for many. The figure of about 15 is an upper bound for these helpers alone: it leaves out the network handling, speech recognition, speech synthesis and the rest of the application on the same core.
- The 8 to 16 kHz resampler is about two thirds of the per-call cost: it filters a zero-stuffed signal, so half its multiplications are by zero. A polyphase version should be cheaper; this was not measured.
- Mu-law decoding is identical to `audioop` for all 256 codes. The mu-law encoder differs from `audioop` on 381 of 65536 input samples (0.6%). All 381 are negative inputs; in the ten cases printed, `audioop` returns the code of the next larger magnitude, and in every one of the 381 cases the prototype's code decodes closer to the original sample. Which of the two matches a reference G.711 encoder was not checked, and `audioop`'s source was not read.

Limits (read before quoting any number):

- One machine, one Python version (3.10). Not run on 3.12 or 3.13, nor on other CPUs.
- The resampler is a naive 31-tap windowed-sinc filter with zero history at frame edges. This script does not test its audio quality or its effect on recognition accuracy, and `audioop`'s sample-rate conversion is a different filter, so only the speed column is comparable.
- Timings of sub-microsecond calls are near the timer's resolution.
- Single-threaded; a multi-call server would share one core and the interpreter lock.

## Reproduce

Open Google Colab, choose a T4 GPU runtime, paste `colab_whisper_baseline.py` into one cell and run it. It stops if the dataset or model licence differs from the expected one.

For the IndicConformer comparison, first accept the model conditions on Hugging Face and store a read token in Colab Secrets as `HF_TOKEN`, then paste `colab_indic_conformer.py` into one cell. It rebuilds the test set and Whisper results itself if the runtime has none. Delete the token afterwards.

For the five-language comparison, use the same Hugging Face token setup and paste `colab_multilang_comparison.py` into one cell (about 25 minutes). It is self-contained and also runs the device check and the default-decoding Whisper check.

For the TTS round trip, accept the conditions on `ai4bharat/indic-parler-tts` and `ai4bharat/indic-conformer-600m-multilingual` with the same Hugging Face account, store the token as `HF_TOKEN`, keep the Colab tab in the foreground at the start, and paste `colab_tts_roundtrip.py` into one cell (about 60 minutes; not yet run as one piece).

## Attribution

FLEURS: Conneau et al., 2022 (CC-BY-4.0). See `packages/ragleap-voice/THIRD_PARTY_NOTICES.md`.

## Per-call timing of transcribe() and Transcriber (one run)

Script: `colab_transcribe_timing.py`. Setup: Tesla T4, Google Colab, 2026-11-07, Python 3.13.16, ragleap-voice 0.3.5, Whisper large-v3 (`Systran/faster-whisper-large-v3`), one clip (first `google/fleurs` en_us test clip, 10.56 s), `device="auto"`, CUDA 12 libraries preloaded with ctypes. One run, in this order.

| Mode | Call | Seconds | RTF |
|---|---|---|---|
| `transcribe()` default | 1 | 13.519 | 1.280 |
| `transcribe()` default | 2 | 3.362 | 0.318 |
| `transcribe()` default | 3 | 3.352 | 0.317 |
| `transcribe()` default | 4 | 3.851 | 0.365 |
| `transcribe()` with `RAGLEAP_VOICE_VERIFY=always` | 1 | 12.222 | 1.157 |
| `transcribe()` with `RAGLEAP_VOICE_VERIFY=always` | 2 | 12.391 | 1.173 |
| `Transcriber` | 1 | 3.623 | 0.343 |
| `Transcriber` | 2 | 0.704 | 0.067 |
| `Transcriber` | 3 | 0.700 | 0.066 |
| `Transcriber` | 4 | 0.694 | 0.066 |

What it shows: the first default call includes the one-time full hash of the model folder and the model open; later default calls skip the hash but still open the model on every call; with `RAGLEAP_VOICE_VERIFY=always` every call hashes the folder again; a `Transcriber` keeps the model open, so calls after its first took about 0.7 s here. All ten calls returned text of the same length (103 characters), which is not an accuracy check.

Limits: one clip, one run, one machine; batch decoding only, not streaming latency; the script does not confirm that the GPU was used (the device option was `auto`); call-to-call differences of a few tenths of a second (for example default call 4) are within what one run cannot separate from noise; the operating system file cache was warm for the later modes. No comparison with any other system is made.

GPU check (`colab_gpu_check.py`, same setup: Tesla T4, Google Colab, 2026-11-07, ragleap-voice 0.3.5, one clip, one run). A `Transcriber` was run with `device` set to `cpu`, `cuda` and `auto`, and `nvidia-smi` was sampled every 0.1 s during three warm calls.

| Device option | GPU memory before, after load (MiB) | Mean and max GPU utilisation | 3 warm calls |
|---|---|---|---|
| `cpu` (control) | 109, 109 | 0% and 0% | 101.83 s |
| `cuda` | 109, 3223 | 92.5% and 100% | 2.13 s |
| `auto` | 109, 3223 | 87.7% and 98% | 2.26 s |

`auto` behaved like `cuda` here (same memory growth, same peak of 3479 MiB, high utilisation), so the timing table above was measured on the GPU, and `cpu` showed no GPU use. GPU memory returned to 109 MiB after the `Transcriber` closed. Limits: one run, 17 samples for each GPU run, memory and utilisation are `nvidia-smi` samples and not a profiler, and `auto` picking the GPU is observed on this setup only.

## Phone-line codec benchmark (one run)

Question: does ragleap's own 8 kHz path (`down2`, mu-law or A-law encode and decode, `up2`) change Whisper large-v3 word error on clean speech, and does it differ from ffmpeg's mu-law? Script: `colab_phone_line_benchmark.py`. Setup: Tesla T4, Google Colab, 2026-11-07, Python 3.13.16, ragleap-voice 0.3.5, Whisper large-v3 (`Systran/faster-whisper-large-v3`), `device="auto"`. Data: the first 30 `google/fleurs` test clips (CC-BY-4.0) for English (en_us), Spanish (es_419) and Russian (ru_ru). Conditions: clean; ffmpeg 8 kHz mu-law and back (as in the noise pilot); ragleap mu-law; ragleap A-law. No added noise and no level normalisation. Intervals are 95% bootstrap intervals over clips (2000 resamples, one seed), and the changes are paired on the same clips. Before any transcription the script checked that both codecs round-trip (SNR 31.8 dB for mu-law and 28.1 dB for A-law on the 8 kHz signal).

| Language | Condition | WER % (95% interval) | Change vs clean (points) | Change vs ffmpeg mu-law (points) |
|---|---|---|---|---|
| English | clean | 5.5 (3.9 to 7.6) | | -0.9 [-2.3, +0.4] |
| English | ffmpeg mu-law | 6.4 (4.5 to 8.8) | +0.9 [-0.4, +2.3] | |
| English | ragleap mu-law | 6.3 (4.3 to 8.8) | +0.7 [-0.6, +2.4] | -0.1 [-1.0, +0.6] |
| English | ragleap A-law | 6.9 (4.9 to 9.3) | +1.3 [-0.3, +3.5] | +0.4 [-0.6, +1.5] |
| Spanish | clean | 2.5 (0.9 to 5.1) | | -0.5 [-1.3, 0.0] |
| Spanish | ffmpeg mu-law | 3.1 (1.3 to 5.7) | +0.5 [0.0, +1.3] | |
| Spanish | ragleap mu-law | 2.8 (1.1 to 5.4) | +0.3 [0.0, +0.7] | -0.3 [-0.9, 0.0] |
| Spanish | ragleap A-law | 3.1 (1.0 to 6.5) | +0.5 [0.0, +1.4] | 0.0 [-1.1, +1.1] |
| Russian | clean | 5.7 (3.5 to 8.6) | | -7.3 [-20.6, +0.4] |
| Russian | ffmpeg mu-law | 13.0 (4.4 to 25.9) | +7.3 [-0.4, +20.6] | |
| Russian | ragleap mu-law | 13.5 (4.5 to 26.5) | +7.8 [-0.2, +21.2] | +0.5 [0.0, +1.6] |
| Russian | ragleap A-law | 13.1 (4.6 to 25.9) | +7.5 [-0.5, +20.9] | +0.2 [-0.5, +1.0] |

What it shows: in all three languages the ragleap mu-law and A-law paths differ from ffmpeg mu-law by -0.3 to +0.5 points, and every interval includes zero or touches it at the edge, so no difference between the ragleap path and ffmpeg's was found at this sample size. Against clean, the telephone-style paths raised word error by 0.3 to 1.3 points in English and Spanish and by 7.3 to 7.8 points in Russian; the English and Russian intervals include zero and the Spanish ones start at zero, so no effect is established with 30 clips. The Russian intervals are very wide; per-clip errors were saved by the script and have not been analysed here.

Limits: a resample plus a codec is not a real phone network (no packet loss, jitter, carrier processing or handset noise); read speech only; 30 clips per language, one run, one seed, wide intervals; word error only; the absolute numbers are not comparable with the noise pilot above, which used a level fix; the speakers and sentences of consecutive clips may repeat. No comparison with any other system is made.

## Gate and Silero false accepts on real recorded noise (script added, not yet run)

`colab_gate_real_noise_benchmark.py` counts false accepts of StreamGate (default floor and -60 dBFS) and Silero VAD on the 843 files of the `free-sound` folder of MUSAN (Snyder, Chen and Povey, 2015; listed as CC BY 4.0 on openslr.org/17; the folder's LICENSE file states that all selected recordings are marked Public Domain on Free Sound). The `sound-bible` folder (listed as Attribution 3.0) is not used. Conditions: as recorded, scaled to -23 dBFS, and the scaled version through ragleap's own 16 to 8 to 16 kHz path for every fourth file. It lists the files Silero accepts so a person can listen to them. No results are published yet. When they are, they will be sound-library recordings, not noise recorded in a room or on a phone line, with no speech mixed in (so nothing about detecting speech), one run, and thresholds that have not been tuned.
