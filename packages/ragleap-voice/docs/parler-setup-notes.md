# Parler engine: setup notes from one report

Status: one maintainer report on Colab, not a guarantee. Sources: issue 990, comments https://github.com/ragleap/ragleap-core/issues/990#issuecomment-6735709968 and https://github.com/ragleap/ragleap-core/issues/990#issuecomment-6741106753 (both 2026-11-09, `ragleap-voice` 0.3.6).

## What happened

The worker environment (Python 3.11, named by `RAGLEAP_VOICE_PARLER_PYTHON`) was built with `parler-tts` from GitHub. The first `synthesize(..., engine="parler")` call failed while loading the tokenizer:

`ImportError: cannot import name 'runtime_version' from 'google.protobuf'`

That environment had resolved `protobuf 4.25.9`, `sentencepiece 0.2.3`, `torch 2.15.0` and `transformers 4.46.1`. The failing import is inside the generated `sentencepiece_model_pb2` of `sentencepiece`.

## What fixed it

Installing `sentencepiece==0.2.0` into the worker environment. Afterwards `uv pip check` reported all 117 packages compatible, the model loaded and audio was generated: 20 clips (English and Hindi, five descriptions, two takes each), 44.1 kHz mono, 1.8 s to 4.6 s long.

## Limits

One install, one run, a Python 3.13 host with a Python 3.11 worker. No timing was recorded. Only `en` and `hi` were run, not `ta`, `te`, `ml` or `kn`. Other combinations of library versions may behave differently. `ragleap-voice` does not install or pin the worker environment, so this is a hint for people who build it, not a requirement.
