# Third-party notices

Every model, dependency or borrowed file that ships or is downloaded by default
must be listed here with its licence and attribution. CC-BY entries must appear
here by model name (enforced by tests/test_licences.py).

Nothing below is shipped in the package or enabled by default. These entries cover
third-party material used only for benchmarking.

## Test data (benchmarks only)
- FLEURS (`google/fleurs` on Hugging Face), CC-BY-4.0. Conneau et al., "FLEURS: Few-shot
  Learning Evaluation of Universal Representations of Speech", 2022 (arXiv:2205.12446).
  Audio is downloaded at run time and not committed.

## Models and packages used for measurement (downloaded at run time, not vendored)
- Whisper large-v3 (OpenAI) converted by Systran (`Systran/faster-whisper-large-v3`),
  MIT per the model card (checked by the benchmark script at run time).
- faster-whisper 1.2.1 (optional extra `whisper`): MIT per its GitHub repository, checked 2026-10-25. CTranslate2 is MIT and PyAV is BSD-3-Clause per their repositories. The licences of its other dependencies (huggingface_hub, tokenizers, onnxruntime, tqdm) have not been reviewed.
- IndicConformer-600M-Multi (AI4Bharat), MIT per the model card and the AI4Bharat GitHub page.
  Gated on Hugging Face and loaded with the repo's own code (`trust_remote_code`); revision
  pinned in `models.toml`. Training-data licences not reviewed.
- transformers, onnxruntime-gpu, onnx, torchaudio: used only inside Colab for measurement;
  licences to be confirmed before any of them becomes a package dependency.
- Indic Parler-TTS (AI4Bharat and Hugging Face), Apache-2.0 per the model card. Gated on Hugging Face
  (account and accepted conditions). Used through the parler-tts library from GitHub at a pinned commit
  (library licence to be confirmed). Training-data licences not reviewed.
- Kokoro-82M (hexgrad), Apache-2.0 per the model card. The card lists CC BY audio among the v1.0 training
  data and has its own attribution section: attribution entries are required before shipping it. Its Hindi
  path (and other non-English paths) uses espeak-ng (GPLv3), installed from the OS package manager for
  measurement only and neither shipped nor imported by this package.
- Chatterbox Multilingual (Resemble AI), MIT per the model card. Outputs carry Resemble's PerTh watermark,
  and the loader initialised the watermarker unconditionally in the version measured. Training-data and
  dependency licences not reviewed.
- google/flan-t5-large (Google), Apache-2.0 per the model card (checked 2026-10-13). Planned dependency of Indic
  Parler-TTS (its config loads this repository's tokenizer at run time); tokenizer files only (5 files), locked at
  revision 0613663d0d48, not yet reviewed.

## Candidate models and their training data (measurement only; licences of the data not reviewed)

These models are listed in `models.toml` as unverified candidates. They are not in the default path and are not vendored. The licence of each model's weights is the one stated on its model card. The training datasets below are taken from the cards; their licences have not been reviewed, and nothing here is legal advice.

- `Axiveri/NaijaVox-2.0` (Apache-2.0 per its card). Training datasets named on the card: google/fleurs (yo_ng, ha_ng, ig_ng), benjaminogbonna/nigerian_accented_english_dataset, asr-nigerian-pidgin/nigerian-pidgin-1.0, Tundragoon/IroyinSpeech, google/WaxalNLP (ha, ig, yo, pcm), benjaminogbonna/nigerian_common_voice_dataset, vpetukhov/bible_tts_hausa. The card says each dataset remains under its own licence.
- `Sunbird/asr-whisper-51-african-languages`, now `Sunbird/SunflowerASR-51-african-languages` (Apache-2.0 per its card). The card lists 29 source collections, among them Mozilla Common Voice, Google FLEURS, Google Waxal, the ASR Africa Data Efficiency Benchmark, African Next Voices, African Voices, NaijaVoices, Shunya Labs and KYAGABA; the full list and per-source licences are on the card and have not been reviewed. One Waxal Amharic set was listed as CC BY-SA 4.0 on its Hugging Face page (share-alike): it needs a legal read before any use for training.
- Both models were evaluated on FLEURS test clips (CC-BY-4.0, attribution recorded above) that their cards list as training data, so measured numbers are possibly contaminated.
